import pytest
from courier.listener.wakeups import StubWakeup, build_wakeup


async def test_stub_wakeup_records_events():
    w = StubWakeup()
    await w.wake({"from": "a", "subject": "hi", "message_id": "m1"})
    assert w.calls == [{"from": "a", "subject": "hi", "message_id": "m1"}]


def test_build_wakeup_selects_strategy():
    assert build_wakeup("stub").__class__.__name__ == "StubWakeup"
    assert build_wakeup("copilot_cli").__class__.__name__ == "CopilotCliWakeup"
    assert build_wakeup("copilot_app").__class__.__name__ == "CopilotAppWakeup"
    with pytest.raises(ValueError):
        build_wakeup("nonsense")


async def test_copilot_cli_builds_command(monkeypatch):
    from courier.listener.wakeups import CopilotCliWakeup
    captured = {}

    async def fake_run(cmd):
        captured["cmd"] = cmd

    w = CopilotCliWakeup(runner=fake_run)
    await w.wake({"from": "cursor", "subject": "Review", "message_id": "m9"})
    assert captured["cmd"][0] == "copilot"
    assert "-p" in captured["cmd"]
    assert any("cursor" in part for part in captured["cmd"])


async def test_copilot_app_builds_deeplink(monkeypatch):
    from courier.listener.wakeups import CopilotAppWakeup
    captured = {}

    async def fake_run(cmd):
        captured["cmd"] = cmd

    w = CopilotAppWakeup(repo="me/repo", runner=fake_run)
    await w.wake({"from": "cli", "subject": "Hi", "message_id": "m1"})
    link = captured["cmd"][-1]
    assert link.startswith("ghapp://session/new?")
    assert "repo=me%2Frepo" in link
    assert "prompt=" in link


async def test_tmux_wakeup_sends_literal_then_enter():
    from courier.listener.wakeups import TmuxWakeup
    cmds = []
    async def fake_run(cmd): cmds.append(cmd)
    w = TmuxWakeup(pane="%7", runner=fake_run)
    await w.wake({"from": "alice", "subject": "review", "message_id": "m1"})
    # first command sends the literal text to the pane, second sends Enter
    assert cmds[0][:4] == ["tmux", "send-keys", "-l", "-t"] and cmds[0][4] == "%7"
    assert "alice" in cmds[0][5]
    assert cmds[1] == ["tmux", "send-keys", "-t", "%7", "Enter"]


def test_build_wakeup_tmux():
    from courier.listener.wakeups import build_wakeup
    w = build_wakeup("tmux", target="%2")
    assert w.__class__.__name__ == "TmuxWakeup" and w.pane == "%2"


import shutil
import asyncio


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
async def test_tmux_wakeup_real_pane_receives_text(tmp_path):
    from courier.listener.wakeups import TmuxWakeup
    session = "courier_test_pane"
    outfile = tmp_path / "out.txt"
    # a pane that writes whatever it reads on stdin into outfile
    await (await asyncio.create_subprocess_exec(
        "tmux", "new-session", "-d", "-s", session, f"cat > {outfile}")).wait()
    try:
        await asyncio.sleep(0.3)
        proc = await asyncio.create_subprocess_exec(
            "tmux", "list-panes", "-t", session, "-F", "#{pane_id}",
            stdout=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        pane = out.decode().split()[0]
        await TmuxWakeup(pane=pane).wake(
            {"from": "alice", "subject": "hi", "message_id": "m1"})
        await asyncio.sleep(0.3)
        await (await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", pane, "C-d")).wait()   # close cat -> flush
        await asyncio.sleep(0.2)
        assert "alice" in outfile.read_text()
    finally:
        await (await asyncio.create_subprocess_exec(
            "tmux", "kill-session", "-t", session)).wait()
