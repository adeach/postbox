"""Fleet mode: manage a fleet of headless agents from one Postbox.

`FleetService` is the durable registry (CRUD over the `fleet_agents` table).
`Supervisor` is the engine: it watches the in-process event bus, and on new mail
spawns a headless turn (`copilot -p "..."`) for each managed identity that has
unread mail — coalesced per identity, capped globally, with crash-loop backoff
and process-group hygiene.

Design invariant (from the spec): the durable inbox is the source of truth; SSE
events are only *hints*. `reconcile()` is the ONLY thing that spawns, and it
decides from the *current* unread state — never directly from an event. That
makes dropped / over-cap / replayed events harmless.
"""
import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from postbox.agents import AgentService
from postbox.auth import now_iso
from postbox.config import Settings
from postbox.db import Database
from postbox.events import EventBus
from postbox.models import FleetAgentOut, RegisterAgent

log = logging.getLogger("postbox.fleet")

DEFAULT_COMMAND = ["copilot", "-p", "{prompt}"]
PROMPT = ("📬 You have unread Postbox mail. Call check_inbox, then read_message and "
          "reply to each unread message, then stop.")
TAIL_LINES = 40


def _iso_in(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


class FleetService:
    """Durable registry of managed agents. A fleet agent IS a registered identity;
    we store its token so the Supervisor can spawn turns that authenticate AS it."""

    def __init__(self, db: Database, agents: AgentService):
        self.db = db
        self.agents = agents

    async def upsert(self, address: str, command: list[str] | None = None,
                     cwd: str | None = None) -> None:
        if cwd is not None and not os.path.isdir(cwd):
            raise ValueError(f"cwd is not a directory: {cwd}")
        command = command or DEFAULT_COMMAND
        if not isinstance(command, list) or not all(isinstance(x, str) for x in command):
            raise ValueError("command must be a list of strings (an arg-list)")

        existing = await self.db.fetchone(
            "SELECT token FROM fleet_agents WHERE address=?", (address,))
        if existing:
            token = existing[0]
        else:
            agent = await self.agents.get_by_address(address)
            if agent is not None:
                # An identity by this name exists but isn't a fleet agent — we never
                # stored its token and cannot recover it, so we can't spawn AS it.
                raise ValueError(
                    f"identity '{address}' already exists and is not a fleet agent; "
                    "pick a new name (or remove that identity first)")
            res = await self.agents.register(RegisterAgent(name=address))
            token = res.token

        await self.db.execute(
            "INSERT INTO fleet_agents(address,token,command_json,cwd,enabled,created_at) "
            "VALUES (?,?,?,?,1,?) "
            "ON CONFLICT(address) DO UPDATE SET command_json=excluded.command_json, "
            "cwd=excluded.cwd",
            (address, token, json.dumps(command), cwd, now_iso()))

    async def remove(self, address: str) -> None:
        await self.db.execute("DELETE FROM fleet_agents WHERE address=?", (address,))

    async def set_enabled(self, address: str, enabled: bool) -> None:
        # Re-enabling clears the crash-loop backoff so the agent gets a fresh chance.
        if enabled:
            await self.db.execute(
                "UPDATE fleet_agents SET enabled=1, fail_count=0, backoff_until=NULL "
                "WHERE address=?", (address,))
        else:
            await self.db.execute(
                "UPDATE fleet_agents SET enabled=0 WHERE address=?", (address,))

    async def exists(self, address: str) -> bool:
        return (await self.db.fetchone(
            "SELECT 1 FROM fleet_agents WHERE address=?", (address,))) is not None

    async def list_rows(self) -> list[dict]:
        rows = await self.db.fetchall(
            "SELECT address,token,command_json,cwd,enabled,fail_count,backoff_until,"
            "last_exit,last_run FROM fleet_agents ORDER BY address")
        keys = ("address", "token", "command_json", "cwd", "enabled", "fail_count",
                "backoff_until", "last_exit", "last_run")
        return [dict(zip(keys, r)) for r in rows]

    async def unread_addresses(self) -> set[str]:
        rows = await self.db.fetchall(
            "SELECT DISTINCT f.address FROM fleet_agents f "
            "JOIN agents a ON a.address=f.address "
            "JOIN recipients r ON r.agent_id=a.id WHERE r.read_at IS NULL")
        return {r[0] for r in rows}

    async def ready_rows(self) -> list[dict]:
        """Enabled fleet agents that have unread mail and are not in active backoff,
        oldest-run first (light fairness so a never-run agent isn't starved)."""
        rows = await self.db.fetchall(
            "SELECT f.address,f.token,f.command_json,f.cwd FROM fleet_agents f "
            "JOIN agents a ON a.address=f.address "
            "WHERE f.enabled=1 AND (f.backoff_until IS NULL OR f.backoff_until<=?) "
            "AND EXISTS (SELECT 1 FROM recipients r "
            "            WHERE r.agent_id=a.id AND r.read_at IS NULL) "
            "ORDER BY (f.last_run IS NULL) DESC, f.last_run ASC",
            (now_iso(),))
        keys = ("address", "token", "command_json", "cwd")
        return [dict(zip(keys, r)) for r in rows]

    async def launch_row(self, address: str) -> dict | None:
        r = await self.db.fetchone(
            "SELECT address,token,command_json,cwd FROM fleet_agents WHERE address=?",
            (address,))
        if r is None:
            return None
        return dict(zip(("address", "token", "command_json", "cwd"), r))

    async def mark_run(self, address: str) -> None:
        await self.db.execute(
            "UPDATE fleet_agents SET last_run=? WHERE address=?", (now_iso(), address))

    async def record_exit(self, address: str, rc: int, s: Settings) -> None:
        row = await self.db.fetchone(
            "SELECT fail_count FROM fleet_agents WHERE address=?", (address,))
        if row is None:
            return
        if rc == 0:
            await self.db.execute(
                "UPDATE fleet_agents SET fail_count=0, backoff_until=NULL, last_exit=?, "
                "last_run=? WHERE address=?", (rc, now_iso(), address))
            return
        fail = row[0] + 1
        backoff = min(s.backoff_cap, s.backoff_base * (2 ** (fail - 1)))
        disable = s.auto_disable_after and fail >= s.auto_disable_after
        await self.db.execute(
            "UPDATE fleet_agents SET fail_count=?, backoff_until=?, last_exit=?, "
            "last_run=?, enabled=? WHERE address=?",
            (fail, _iso_in(backoff), rc, now_iso(), 0 if disable else 1, address))


class _Turn:
    __slots__ = ("proc", "started", "tail", "task", "buf")

    def __init__(self, proc=None):
        self.proc = proc                                  # None while the slot is reserved
        self.started = time.monotonic()
        self.tail: deque[str] = deque(maxlen=TAIL_LINES)
        self.task: asyncio.Task | None = None
        self.buf = ""                                     # partial (newline-less) output tail


class Supervisor:
    """Spawns headless turns on new mail. Runs in-process with the server."""

    def __init__(self, db: Database, bus: EventBus, fleet: FleetService,
                 settings: Settings, spawn=None):
        self.db = db
        self.bus = bus
        self.fleet = fleet
        self.s = settings
        self._spawn = spawn or self._spawn_subprocess     # seam for tests
        self.running: dict[str, _Turn] = {}
        self._tails: dict[str, str] = {}
        self._last_run: dict[str, float] = {}             # monotonic, per-agent cooldown
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._fh: asyncio.Queue | None = None
        self._stopped = False

    # ----- lifecycle -----
    async def start(self) -> None:
        # Subscribe to the firehose from NOW so a restart never replays history into
        # a spawn storm; the startup reconcile below catches any mail missed while down.
        self._fh = self.bus.subscribe_all()
        self._tasks = [
            asyncio.create_task(self._firehose_loop()),
            asyncio.create_task(self._reconcile_loop()),
        ]
        self.poke()

    async def stop(self) -> None:
        self._stopped = True
        self.poke()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        if self._fh is not None:
            self.bus.unsubscribe_all(self._fh)
        # Terminate live turns, then AWAIT their supervisors so each turn's final
        # record_exit() commits before the caller (lifespan) closes the DB.
        turn_tasks = [tn.task for tn in self.running.values() if tn.task]
        for tn in list(self.running.values()):
            if tn.proc is not None:
                await self._terminate(tn.proc)
        if turn_tasks:
            await asyncio.gather(*turn_tasks, return_exceptions=True)

    def poke(self) -> None:
        self._wake.set()

    # ----- trigger loops (never spawn directly) -----
    async def _firehose_loop(self) -> None:
        assert self._fh is not None
        while not self._stopped:
            ev = await self._fh.get()
            if ev.type == "message.received":
                self.poke()

    async def _reconcile_loop(self) -> None:
        while not self._stopped:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.s.reconcile_interval)
            self._wake.clear()
            if self._stopped:
                break
            await self._reap_overruns()
            try:
                await self.reconcile()
            except Exception:
                log.exception("reconcile failed")

    # ----- the engine -----
    async def reconcile(self) -> None:
        if self._stopped:
            return
        for row in await self.fleet.ready_rows():
            if len(self.running) >= self.s.max_concurrent:
                break                                     # slot busy — durable, retried next reconcile
            address = row["address"]
            if address in self.running:
                continue                                  # coalesce: one turn per identity
            last = self._last_run.get(address)
            if last is not None and (time.monotonic() - last) < self.s.agent_cooldown:
                continue                                  # bounds even an exit-0 no-op loop
            self.running[address] = _Turn()               # reserve the slot SYNCHRONOUSLY (before any await)
            await self._launch(row, address)

    async def run_now(self, address: str) -> str:
        """Force a turn now (UI 'Run'): ignores unread/cooldown/backoff, still
        respects the global cap and one-turn-per-identity."""
        if address in self.running:
            return "already-running"
        if len(self.running) >= self.s.max_concurrent:
            return "at-capacity"
        self.running[address] = _Turn()                   # reserve BEFORE the first await (no double-spawn)
        try:
            row = await self.fleet.launch_row(address)
        except BaseException:
            self.running.pop(address, None)               # never leave a wedged reservation
            raise
        if row is None:
            self.running.pop(address, None)
            raise KeyError(address)
        await self._launch(row, address)
        return "started"

    async def kill(self, address: str) -> bool:
        turn = self.running.get(address)
        if turn is None or turn.proc is None:
            return False
        await self._terminate(turn.proc)
        return True

    async def _launch(self, row: dict, address: str) -> None:
        """Fill the pre-reserved slot at self.running[address] with a live turn.
        A `finally` releases the reservation on ANY non-success — exception,
        cancellation (client disconnects the /run request), or shutdown — and reaps a
        child that was spawned but never supervised. So the slot can never wedge."""
        proc = None
        created = False
        try:
            template = json.loads(row["command_json"])
            argv = [a.replace("{prompt}", PROMPT) for a in template]
            env = {**os.environ, "POSTBOX_TOKEN": row["token"],
                   "POSTBOX_URL": self.s.public_url}
            env.pop("POSTBOX_NAME", None)             # token wins; don't let a name fight it
            self._last_run[address] = time.monotonic()
            await self.fleet.mark_run(address)
            proc = await self._spawn(argv, row["cwd"], env)
            turn = self.running.get(address)
            if turn is None or self._stopped:         # reservation cleared / shutting down
                return                                # finally reaps proc + releases slot
            turn.proc = proc
            turn.started = time.monotonic()
            turn.task = asyncio.create_task(self._supervise(address, turn))
            created = True
        except Exception as e:                        # bad binary, DB error (NOT cancellation)
            log.warning("launch failed for %s: %r", address, e)
            self._tails[address] = f"launch error: {e}"
            with contextlib.suppress(Exception):
                await self.fleet.record_exit(address, 127, self.s)
        finally:
            if not created:
                self.running.pop(address, None)       # release reservation (sync — survives cancel)
                if proc is not None:
                    with contextlib.suppress(Exception):
                        await self._terminate(proc)   # reap a spawned-but-unsupervised child
                self.poke()

    def _append_tail(self, turn: _Turn, chunk: bytes) -> None:
        """Bounded output tail: cap line length AND buffer growth so a single huge
        newline-less line can't blow up memory."""
        turn.buf += chunk.decode(errors="replace")
        while "\n" in turn.buf:
            line, turn.buf = turn.buf.split("\n", 1)
            turn.tail.append(line[:2000])
        if len(turn.buf) > 4096:                          # long line, no newline yet — flush a bounded slice
            turn.tail.append(turn.buf[:2000])
            turn.buf = ""

    async def _supervise(self, address: str, turn: _Turn) -> None:
        rc = -1
        try:
            try:
                if turn.proc.stdout is not None:
                    # Read fixed-size CHUNKS (not lines): readline raises on a >64 KB
                    # line, which would strand the child. Chunks always drain to EOF.
                    while True:
                        chunk = await turn.proc.stdout.read(4096)
                        if not chunk:
                            break
                        self._append_tail(turn, chunk)
            finally:
                rc = await turn.proc.wait()               # ALWAYS reap, even if draining raised
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("turn for %s errored: %r", address, e)
        if turn.buf:
            turn.tail.append(turn.buf[:2000])
        if self.running.get(address) is turn:             # pop only if WE still own the slot
            self.running.pop(address, None)
        if turn.tail:
            self._tails[address] = "\n".join(turn.tail)
        await self.fleet.record_exit(address, rc, self.s)
        self.poke()                                       # a slot freed → re-drive the queue

    async def _reap_overruns(self) -> None:
        now = time.monotonic()
        for turn in list(self.running.values()):
            if turn.proc is not None and now - turn.started > self.s.max_runtime:
                await self._terminate(turn.proc)

    async def _spawn_subprocess(self, argv: list[str], cwd: str | None, env: dict):
        return await asyncio.create_subprocess_exec(
            *argv, cwd=cwd or None, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True)                       # own process group for group-kill

    async def _terminate(self, proc) -> None:
        if proc.returncode is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError):
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)

    # ----- status for the UI -----
    async def list_status(self) -> list[FleetAgentOut]:
        rows = await self.fleet.list_rows()
        unread = await self.fleet.unread_addresses()
        now = now_iso()
        out = []
        for r in rows:
            addr = r["address"]
            if not r["enabled"]:
                state = "disabled"
            elif addr in self.running:
                state = "running"
            elif r["backoff_until"] and r["backoff_until"] > now:
                state = "backoff"
            elif addr in unread:
                state = "queued"
            else:
                state = "idle"
            out.append(FleetAgentOut(
                address=addr, enabled=bool(r["enabled"]), state=state,
                command=json.loads(r["command_json"]), cwd=r["cwd"],
                fail_count=r["fail_count"], last_exit=r["last_exit"],
                last_run=r["last_run"], backoff_until=r["backoff_until"],
                tail=self._tails.get(addr, "")))
        return out
