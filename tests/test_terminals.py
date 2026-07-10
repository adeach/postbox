from types import SimpleNamespace

import asyncio

import pytest

from postbox.agents import AgentService
from postbox.models import RegisterAgent
from postbox.terminals import TerminalService


class FakeRunner:
    """Records argv and returns a canned (rc, output) — no real tmux.
    `has_session` controls the has-session probe: False = shared session absent (first
    agent → new-session), True = present (next agents → new-window)."""
    def __init__(self, rc=0, out="", has_session=False):
        self.calls = []
        self.rc = rc
        self.out = out
        self.has_session = has_session

    async def __call__(self, argv):
        self.calls.append(argv)
        if argv[:2] == ["tmux", "has-session"]:
            return (0 if self.has_session else 1), ""
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
    assert res == {"name": "alice", "session": "postbox_main", "project": "main",
                   "window": "alice",
                   "attach": "tmux attach -t postbox_main \\; select-window -t alice"}
    # first agent of a project CREATES its session with its window; argv is an exec list (no shell):
    # tmux → detached named session → env-set vars → copilot with ALL perms pre-approved
    assert r.calls[-1] == [
        "tmux", "new-session", "-d", "-s", "postbox_main", "-n", "alice",
        "env", "POSTBOX_NAME=alice", "POSTBOX_URL=http://127.0.0.1:8765",
        "copilot", "--allow-all", "--allow-all-mcp-server-instructions"]


async def test_teammate_is_a_window_in_project_session(db):
    r = FakeRunner(has_session=True)          # the project session already exists
    res = await _svc(db, r).spawn("beta", project="authfeat")
    assert res["session"] == "postbox_authfeat" and res["project"] == "authfeat"
    assert res["window"] == "beta"
    # next teammates are ADDED as a window in the SAME project session, not a new session
    assert r.calls[-1] == [
        "tmux", "new-window", "-t", "postbox_authfeat", "-n", "beta",
        "env", "POSTBOX_NAME=beta", "POSTBOX_URL=http://127.0.0.1:8765",
        "copilot", "--allow-all", "--allow-all-mcp-server-instructions"]


async def test_first_agent_of_project_creates_named_session(db):
    r = FakeRunner()                          # no session yet
    res = await _svc(db, r).spawn("lead", project="myproj")
    assert res["session"] == "postbox_myproj"
    assert r.calls[-1][:6] == ["tmux", "new-session", "-d", "-s", "postbox_myproj", "-n"]


@pytest.mark.parametrize("bad", ["has space", "dot.name", "colon:x", "a" * 41, "semi;rm"])
async def test_spawn_rejects_bad_project(db, bad):
    r = FakeRunner()
    with pytest.raises(ValueError):
        await _svc(db, r).spawn("worker", project=bad)
    assert r.calls == []


async def test_spawn_default_runs_autonomously(db):
    r = FakeRunner()
    await _svc(db, r).spawn("alice")
    assert "--allow-all" in r.calls[-1]     # tools+paths+urls: no prompt, worker acts unattended


async def test_spawn_with_cwd_adds_c_flag(db, tmp_path):
    r = FakeRunner()
    svc = _svc(db, r)
    await svc.spawn("bob", cwd=str(tmp_path))
    argv = r.calls[-1]
    assert argv[7:9] == ["-c", str(tmp_path)]      # -c comes after `-n <name>`, before the `env` program


async def test_spawn_program_seam(db):
    r = FakeRunner()
    svc = _svc(db, r, program=("sleep", "5"))
    await svc.spawn("carol")
    assert r.calls[-1][-2:] == ["sleep", "5"]       # launched program is swappable for tests


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


async def test_list_windows_grouped_by_project(db):
    # `tmux list-windows -a` output: "<session> <window>" per line, across all sessions
    r = FakeRunner(out="postbox_main bob\npostbox_main alice\npostbox_authfeat reviewer\nother-sess x\n")
    got = await _svc(db, r).list_terminals()
    # only postbox_* windows, sorted by (project, window); 'other-sess' filtered out
    assert [(t["project"], t["name"]) for t in got] == [
        ("authfeat", "reviewer"), ("main", "alice"), ("main", "bob")]
    rev = got[0]
    assert rev["session"] == "postbox_authfeat"
    assert rev["attach"] == "tmux attach -t postbox_authfeat \\; select-window -t reviewer"


async def test_list_empty_when_no_tmux_server(db):
    r = FakeRunner(rc=1, out="no server running on /tmp/tmux-501/default")
    assert await _svc(db, r).list_terminals() == []


async def test_kill_finds_window_across_projects_and_validates(db):
    # kill scans `list-windows -a` to find which project session holds the window
    r = FakeRunner(out="postbox_authfeat alice\npostbox_main bob\n")
    svc = _svc(db, r)
    await svc.kill("alice")
    assert r.calls[-1] == ["tmux", "kill-window", "-t", "postbox_authfeat:alice"]
    with pytest.raises(ValueError):
        await svc.kill("bad name")


async def test_kill_unknown_window_raises(db):
    r = FakeRunner(out="postbox_main bob\n")
    with pytest.raises(RuntimeError):
        await _svc(db, r).kill("ghost")             # no such window anywhere


async def test_reap_force_kills_hung_survivor(db):
    """kill-window SIGHUPs a healthy copilot; a hung one that ignores it is SIGKILLed."""
    proc = await asyncio.create_subprocess_exec("sleep", "60")
    await _svc(db, FakeRunner())._reap(str(proc.pid))
    await asyncio.wait_for(proc.wait(), timeout=3)   # SIGKILL delivered → it exits
    assert proc.returncode is not None


async def test_reap_is_noop_on_dead_or_bad_pid(db):
    svc = _svc(db, FakeRunner())
    await svc._reap(None)                # no pid → nothing
    await svc._reap("not-a-number")      # unparseable → nothing
    await svc._reap("2147480000")        # (almost certainly) dead pid → no raise


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
            assert d["session"] == "postbox_main" and d["window"] == "helper"
            assert d["attach"] == "tmux attach -t postbox_main \\; select-window -t helper"
            assert d["registered"] is False          # no real copilot registered it
            # project groups the team: session name is postbox_<project>
            rp = await c.post("/spawn", headers=h, json={"name": "helper3", "project": "feat"})
            assert rp.status_code == 201
            assert rp.json()["session"] == "postbox_feat" and rp.json()["project"] == "feat"
            # no bearer token → 401 (not open like the observer UI route)
            assert (await c.post("/spawn", json={"name": "helper2"})).status_code == 401
            # collision with the caller's own name → 409
            assert (await c.post("/spawn", headers=h,
                                 json={"name": "caller"})).status_code == 409


async def test_federation_spawn_endpoint_peer_token_gated(tmp_path):
    """POST /federation/spawn: a peer asks us to spawn locally — peer-token gated."""
    from httpx import ASGITransport, AsyncClient
    from postbox.api import create_app
    app = create_app(str(tmp_path / "data"))
    async with app.router.lifespan_context(app):
        app.state.terminals = TerminalService(
            SimpleNamespace(port=8765), app.state.agents, runner=FakeRunner())
        app.state.terminals.spawn_wait = 0.2
        await app.state.peers.upsert("laptop", "http://laptop", "peer-tok")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            # unknown token → 401
            r = await c.post("/federation/spawn",
                             headers={"X-Postbox-Peer-Token": "nope"},
                             json={"name": "helper"})
            assert r.status_code == 401
            # valid peer token → spawns
            r = await c.post("/federation/spawn",
                             headers={"X-Postbox-Peer-Token": "peer-tok"},
                             json={"name": "helper"})
            assert r.status_code == 201
            assert r.json()["session"] == "postbox_main" and r.json()["window"] == "helper"


async def test_spawn_endpoint_routes_remote_instance_to_federation(tmp_path):
    """POST /spawn with a remote `instance` delegates to federation.spawn_remote."""
    from httpx import ASGITransport, AsyncClient
    from postbox.api import create_app
    app = create_app(str(tmp_path / "data"))
    async with app.router.lifespan_context(app):
        await app.state.peers.upsert("vm", "http://vm", "tok")
        calls = []
        async def fake_spawn_relay(url, token, payload):
            calls.append((url, token, payload))
            return {"name": payload["name"], "session": "postbox_x",
                    "attach": "tmux attach -t postbox_x", "registered": True}
        app.state.federation.spawn_relay = fake_spawn_relay
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            caller = (await c.post("/agents", json={"name": "caller"})).json()
            h = {"Authorization": f"Bearer {caller['token']}"}
            r = await c.post("/spawn", headers=h,
                             json={"name": "helper", "instance": "vm"})
            assert r.status_code == 201
            d = r.json()
            assert d["address"] == "helper@vm" and d["instance"] == "vm"
            assert calls and calls[0][0] == "http://vm"
            # unknown instance → 404
            r2 = await c.post("/spawn", headers=h,
                              json={"name": "helper2", "instance": "ghost"})
            assert r2.status_code == 404


async def test_spawn_with_model_inserts_model_flag(db):
    r = FakeRunner()
    svc = _svc(db, r)
    await svc.spawn("worker", model="claude-opus-4.8")
    argv = r.calls[-1]
    # `--model X` sits right after the copilot binary, before its other flags
    i = argv.index("copilot")
    assert argv[i:i+3] == ["copilot", "--model", "claude-opus-4.8"]
    assert "--allow-all" in argv


async def test_spawn_without_model_has_no_model_flag(db):
    r = FakeRunner()
    await _svc(db, r).spawn("worker")
    assert "--model" not in r.calls[-1]


@pytest.mark.parametrize("bad", ["has space", "semi;rm", "a" * 61, "bad/slash"])
async def test_spawn_rejects_bad_model(db, bad):
    r = FakeRunner()
    with pytest.raises(ValueError):
        await _svc(db, r).spawn("worker", model=bad)
    assert r.calls == []
