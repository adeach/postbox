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
