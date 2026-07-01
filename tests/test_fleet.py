import asyncio
import json
import os
import sys
from dataclasses import replace

import pytest

from postbox.agents import AgentService
from postbox.auth import now_iso
from postbox.config import Settings
from postbox.events import EventBus
from postbox.fleet import PROMPT, FleetService, Supervisor, _iso_in
from postbox.messages import MessageService
from postbox.models import RegisterAgent, SendMessage


def mk_settings(tmp_path, **over) -> Settings:
    base = Settings(data_dir=tmp_path, db_path=tmp_path / "x.db")
    return replace(base, **over)


async def build(db, settings, spawn=None):
    agents = AgentService(db)
    bus = EventBus(db)
    messages = MessageService(db, agents, bus)
    fleet = FleetService(db, agents)
    sup = Supervisor(db, bus, fleet, settings, spawn=spawn)
    return agents, bus, messages, fleet, sup


async def send_to(agents, messages, to_addr, n=1, sender="snd"):
    s = await agents.register(RegisterAgent(name=sender))
    for i in range(n):
        await messages.send(s.id, SendMessage(to=to_addr, body=f"m{i}"))


async def mark_read(db, agents, address):
    a = await agents.get_by_address(address)
    await db.execute("UPDATE recipients SET read_at=? WHERE agent_id=?",
                     (now_iso(), a.id))


class FakeProc:
    """A subprocess stand-in whose exit the test controls via finish()."""
    def __init__(self, rc=0):
        self.returncode = None
        self._rc = rc
        self.stdout = None
        self.pid = -1
        self._exit = asyncio.Event()

    async def wait(self):
        await self._exit.wait()
        self.returncode = self._rc
        return self._rc

    def finish(self, rc=None):
        if rc is not None:
            self._rc = rc
        self._exit.set()


class Stub:
    def __init__(self, rc=0):
        self.calls = []
        self.procs = []
        self.rc = rc

    async def __call__(self, argv, cwd, env):
        p = FakeProc(self.rc)
        self.calls.append({"argv": argv, "cwd": cwd, "env": env})
        self.procs.append(p)
        return p


async def finish_all(sup, stub):
    """Teardown: complete any still-running fake turns and await their supervisors."""
    tasks = [t.task for t in sup.running.values() if t.task]
    for p in stub.procs:
        if not p._exit.is_set():
            p.finish(0)
    for t in tasks:
        try:
            await t
        except Exception:
            pass


# ----- FleetService (registry) -----

async def test_upsert_registers_identity_and_token_resolves(db, tmp_path):
    agents, _, _, fleet, _ = await build(db, mk_settings(tmp_path))
    await fleet.upsert("alice")
    assert await agents.get_by_address("alice") is not None
    tok, cmd = await db.fetchone(
        "SELECT token, command_json FROM fleet_agents WHERE address=?", ("alice",))
    # the stored token authenticates AS alice — this is the identity-injection contract
    who = await agents.resolve_token(tok)
    assert who.address == "alice"
    assert json.loads(cmd) == ["copilot", "-p", "{prompt}"]


async def test_upsert_refuses_existing_nonfleet_identity(db, tmp_path):
    agents, _, _, fleet, _ = await build(db, mk_settings(tmp_path))
    await agents.register(RegisterAgent(name="bob"))   # a plain identity, not fleet
    with pytest.raises(ValueError):
        await fleet.upsert("bob")


async def test_upsert_rejects_bad_cwd(db, tmp_path):
    _, _, _, fleet, _ = await build(db, mk_settings(tmp_path))
    with pytest.raises(ValueError):
        await fleet.upsert("alice", cwd="/no/such/dir/xyz")


# ----- Supervisor scheduling -----

async def test_reconcile_coalesces_per_identity(db, tmp_path):
    stub = Stub()
    agents, _, messages, fleet, sup = await build(
        db, mk_settings(tmp_path, agent_cooldown=0), spawn=stub)
    await fleet.upsert("alice")
    await send_to(agents, messages, "alice", n=3)     # 3 messages...
    await sup.reconcile()
    assert len(stub.calls) == 1 and "alice" in sup.running   # ...one turn
    # generic prompt substituted; token injected as env
    assert stub.calls[0]["argv"] == ["copilot", "-p", PROMPT]
    assert "POSTBOX_TOKEN" in stub.calls[0]["env"]
    await sup.reconcile()                              # still running → no double-spawn
    assert len(stub.calls) == 1
    await finish_all(sup, stub)


async def test_cap_limits_and_queue_drains(db, tmp_path):
    stub = Stub()
    agents, _, messages, fleet, sup = await build(
        db, mk_settings(tmp_path, max_concurrent=1, agent_cooldown=0), spawn=stub)
    await fleet.upsert("alice")
    await fleet.upsert("bob")
    await send_to(agents, messages, "alice")
    await send_to(agents, messages, "bob", sender="snd2")
    await sup.reconcile()
    assert len(sup.running) == 1                       # cap respected
    first = next(iter(sup.running))
    turn = sup.running[first]
    await mark_read(db, agents, first)                 # the turn read its mail...
    stub.procs[0].finish(0)                            # ...and exited
    await turn.task
    assert first not in sup.running
    await sup.reconcile()                              # slot freed → the other launches
    assert len(sup.running) == 1
    second = next(iter(sup.running))
    assert {first, second} == {"alice", "bob"}
    await finish_all(sup, stub)


async def test_reconcile_skips_disabled_and_backoff(db, tmp_path):
    stub = Stub()
    agents, _, messages, fleet, sup = await build(db, mk_settings(tmp_path), spawn=stub)
    await fleet.upsert("dis")
    await fleet.upsert("back")
    await send_to(agents, messages, "dis")
    await send_to(agents, messages, "back", sender="snd2")
    await fleet.set_enabled("dis", False)
    await db.execute("UPDATE fleet_agents SET backoff_until=? WHERE address=?",
                     (_iso_in(999), "back"))
    await sup.reconcile()
    assert stub.calls == []                            # neither eligible


async def test_run_now_ignores_unread_and_unknown_raises(db, tmp_path):
    stub = Stub()
    _, _, _, fleet, sup = await build(db, mk_settings(tmp_path), spawn=stub)
    await fleet.upsert("alice")                        # no mail
    assert await sup.run_now("alice") == "started"
    assert len(stub.calls) == 1
    with pytest.raises(KeyError):
        await sup.run_now("ghost")
    await finish_all(sup, stub)


# ----- backoff / crash-loop policy -----

async def test_backoff_grows_then_auto_disables_then_resets(db, tmp_path):
    s = mk_settings(tmp_path, auto_disable_after=3, backoff_base=5, backoff_cap=100)
    _, _, _, fleet, _ = await build(db, s)
    await fleet.upsert("alice")
    for i in (1, 2):
        await fleet.record_exit("alice", 1, s)
        fail, backoff, enabled = await db.fetchone(
            "SELECT fail_count, backoff_until, enabled FROM fleet_agents WHERE address=?",
            ("alice",))
        assert fail == i and backoff is not None and enabled == 1
    await fleet.record_exit("alice", 1, s)             # 3rd consecutive failure
    fail, enabled = await db.fetchone(
        "SELECT fail_count, enabled FROM fleet_agents WHERE address=?", ("alice",))
    assert fail == 3 and enabled == 0                  # auto-disabled
    await fleet.set_enabled("alice", True)             # re-enable clears backoff
    await fleet.record_exit("alice", 0, s)             # a clean turn resets
    fail, backoff = await db.fetchone(
        "SELECT fail_count, backoff_until FROM fleet_agents WHERE address=?", ("alice",))
    assert fail == 0 and backoff is None


# ----- process hygiene: real process-group kill -----

async def test_group_kill_terminates_real_process(db, tmp_path):
    _, _, _, fleet, sup = await build(db, mk_settings(tmp_path))   # real spawn
    await fleet.upsert("sleeper",
                       command=[sys.executable, "-c", "import time; time.sleep(30)"])
    assert await sup.run_now("sleeper") == "started"
    turn = sup.running["sleeper"]
    pid = turn.proc.pid
    os.kill(pid, 0)                                    # alive (no exception)
    await sup.kill("sleeper")
    await turn.task
    assert "sleeper" not in sup.running
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)                                # gone


# ----- status projection for the UI -----

async def test_list_status_projects_states(db, tmp_path):
    stub = Stub()
    agents, _, messages, fleet, sup = await build(
        db, mk_settings(tmp_path, agent_cooldown=0), spawn=stub)
    await fleet.upsert("idle1")
    await fleet.upsert("queued1")
    await fleet.upsert("disabled1")
    await send_to(agents, messages, "queued1")
    await fleet.set_enabled("disabled1", False)
    st = {x.address: x.state for x in await sup.list_status()}
    assert st == {"idle1": "idle", "queued1": "queued", "disabled1": "disabled"}
    await sup.run_now("idle1")
    st = {x.address: x.state for x in await sup.list_status()}
    assert st["idle1"] == "running"
    await finish_all(sup, stub)
