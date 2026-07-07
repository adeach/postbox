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


def _svc(db, runner, program=("copilot",)):
    return TerminalService(SimpleNamespace(port=8765), AgentService(db),
                           runner=runner, program=program)


async def test_spawn_builds_injection_safe_tmux_argv(db):
    r = FakeRunner()
    svc = _svc(db, r)
    res = await svc.spawn("alice")
    assert res == {"name": "alice", "session": "postbox-alice",
                   "attach": "tmux attach -t postbox-alice"}
    # argv is an exec list (no shell): tmux → detached named session → env-set vars → copilot
    assert r.calls[0] == [
        "tmux", "new-session", "-d", "-s", "postbox-alice",
        "env", "POSTBOX_NAME=alice", "POSTBOX_URL=http://127.0.0.1:8765", "copilot"]


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


async def test_spawn_rejects_existing_online_agent_but_allows_forgotten(db):
    agents = AgentService(db)
    a = await agents.register(RegisterAgent(name="alice"))     # status='online'
    r = FakeRunner()
    svc = TerminalService(SimpleNamespace(port=8765), agents, runner=r)
    with pytest.raises(ValueError):
        await svc.spawn("alice")                    # name in use → reject
    await agents.deregister(a.id)                   # 'forget' it
    res = await svc.spawn("alice")                  # now the name is reusable
    assert res["session"] == "postbox-alice"


async def test_spawn_surfaces_tmux_failure(db):
    r = FakeRunner(rc=1, out="duplicate session: postbox-alice")
    with pytest.raises(RuntimeError):
        await _svc(db, r).spawn("alice")


async def test_list_filters_postbox_sessions(db):
    r = FakeRunner(out="postbox-bob\npostbox-alice\nother-session\nmy-work\n")
    got = await _svc(db, r).list_terminals()
    assert [t["name"] for t in got] == ["alice", "bob"]     # only postbox-*, sorted
    assert got[0]["attach"] == "tmux attach -t postbox-alice"


async def test_list_empty_when_no_tmux_server(db):
    r = FakeRunner(rc=1, out="no server running on /tmp/tmux-501/default")
    assert await _svc(db, r).list_terminals() == []


async def test_kill_builds_argv_and_validates(db):
    r = FakeRunner()
    svc = _svc(db, r)
    await svc.kill("alice")
    assert r.calls[0] == ["tmux", "kill-session", "-t", "postbox-alice"]
    with pytest.raises(ValueError):
        await svc.kill("bad name")
