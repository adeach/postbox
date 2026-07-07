from types import SimpleNamespace

import pytest

from postbox.agents import AgentService
from postbox.models import RegisterAgent
from postbox.terminals import TerminalService


class FakeRunner:
    """Records argv and returns a canned (rc, output) — no real tmux."""
    def __init__(self, rc=0, out=""):
        self.calls = []
        self.rc = rc
        self.out = out

    async def __call__(self, argv):
        self.calls.append(argv)
        return self.rc, self.out


def _svc(db, runner, program=None):
    kw = {"runner": runner}
    if program is not None:
        kw["program"] = program          # else use TerminalService's real default
    return TerminalService(SimpleNamespace(port=8765), AgentService(db), **kw)


async def test_spawn_builds_injection_safe_tmux_argv(db):
    r = FakeRunner()
    svc = _svc(db, r)
    res = await svc.spawn("alice")
    assert res == {"name": "alice", "session": "postbox_alice",
                   "attach": "tmux attach -t postbox_alice"}
    # argv is an exec list (no shell): tmux → detached named session → env-set vars →
    # copilot with postbox tools pre-approved (so the spawned agent never prompts)
    assert r.calls[0] == [
        "tmux", "new-session", "-d", "-s", "postbox_alice",
        "env", "POSTBOX_NAME=alice", "POSTBOX_URL=http://127.0.0.1:8765",
        "copilot", "--allow-tool=postbox"]


async def test_spawn_default_preapproves_postbox_tools(db):
    r = FakeRunner()
    await _svc(db, r).spawn("alice")
    assert "--allow-tool=postbox" in r.calls[0]     # no permission prompt for postbox MCP


async def test_spawn_with_cwd_adds_c_flag(db, tmp_path):
    r = FakeRunner()
    svc = _svc(db, r)
    await svc.spawn("bob", cwd=str(tmp_path))
    argv = r.calls[0]
    assert argv[5:7] == ["-c", str(tmp_path)]      # -c comes before the `env` program


async def test_spawn_program_seam(db):
    r = FakeRunner()
    svc = _svc(db, r, program=("sleep", "5"))
    await svc.spawn("carol")
    assert r.calls[0][-2:] == ["sleep", "5"]       # launched program is swappable for tests


@pytest.mark.parametrize("bad", ["", "has space", "dot.name", "colon:x", "a" * 41, "semi;rm"])
async def test_spawn_rejects_bad_name(db, bad):
    r = FakeRunner()
    with pytest.raises(ValueError):
        await _svc(db, r).spawn(bad)
    assert r.calls == []                            # nothing spawned on a bad name


async def test_spawn_rejects_bad_cwd(db):
    r = FakeRunner()
    with pytest.raises(ValueError):
        await _svc(db, r).spawn("alice", cwd="/no/such/dir/xyz")
    assert r.calls == []


async def test_spawn_rejects_existing_name_online_or_forgotten(db):
    agents = AgentService(db)
    a = await agents.register(RegisterAgent(name="alice"))     # status='online'
    r = FakeRunner()
    svc = TerminalService(SimpleNamespace(port=8765), agents, runner=r)
    with pytest.raises(ValueError):
        await svc.spawn("alice")                    # online name → reject
    await agents.deregister(a.id)                   # 'forget' it
    with pytest.raises(ValueError):
        await svc.spawn("alice")                    # STILL reserved (would 409 at register)
    assert r.calls == []                            # nothing spawned either time


async def test_wait_registered(db):
    agents = AgentService(db)
    svc = TerminalService(SimpleNamespace(port=8765), agents, runner=FakeRunner())
    assert await svc.wait_registered("ghost", timeout=0.15) is False   # never appears
    await agents.register(RegisterAgent(name="real"))
    assert await svc.wait_registered("real", timeout=0.15) is True


async def test_spawn_surfaces_tmux_failure(db):
    r = FakeRunner(rc=1, out="duplicate session: postbox_alice")
    with pytest.raises(RuntimeError):
        await _svc(db, r).spawn("alice")


async def test_list_filters_postbox_sessions(db):
    r = FakeRunner(out="postbox_bob\npostbox_alice\nother-session\nmy-work\n")
    got = await _svc(db, r).list_terminals()
    assert [t["name"] for t in got] == ["alice", "bob"]     # only postbox_*, sorted
    assert got[0]["attach"] == "tmux attach -t postbox_alice"


async def test_list_empty_when_no_tmux_server(db):
    r = FakeRunner(rc=1, out="no server running on /tmp/tmux-501/default")
    assert await _svc(db, r).list_terminals() == []


async def test_kill_builds_argv_and_validates(db):
    r = FakeRunner()
    svc = _svc(db, r)
    await svc.kill("alice")
    assert r.calls[0] == ["tmux", "kill-session", "-t", "postbox_alice"]
    with pytest.raises(ValueError):
        await svc.kill("bad name")


async def test_spawn_endpoint_is_bearer_authed(tmp_path):
    """POST /spawn: any registered AGENT can spin up a terminal (Bearer, not observer),
    and the response includes the tmux attach cmd + whether it registered in time."""
    from httpx import ASGITransport, AsyncClient
    from postbox.api import create_app
    app = create_app(str(tmp_path / "data"))
    async with app.router.lifespan_context(app):
        # swap in a fake-runner terminal service (no real tmux/copilot) with a short wait
        app.state.terminals = TerminalService(
            SimpleNamespace(port=8765), app.state.agents, runner=FakeRunner())
        app.state.terminals.spawn_wait = 0.2
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            caller = (await c.post("/agents", json={"name": "caller"})).json()
            h = {"Authorization": f"Bearer {caller['token']}"}
            r = await c.post("/spawn", headers=h, json={"name": "helper"})
            assert r.status_code == 201
            d = r.json()
            assert d["session"] == "postbox_helper"
            assert d["attach"] == "tmux attach -t postbox_helper"
            assert d["registered"] is False          # no real copilot registered it
            # no bearer token → 401 (not open like the observer UI route)
            assert (await c.post("/spawn", json={"name": "helper2"})).status_code == 401
            # collision with the caller's own name → 409
            assert (await c.post("/spawn", headers=h,
                                 json={"name": "caller"})).status_code == 409
