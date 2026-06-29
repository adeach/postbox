import asyncio
import shlex
from urllib.parse import quote, urlencode


def _notification_text(event: dict) -> str:
    subj = event.get("subject") or "(no subject)"
    return (f"📬 New mail from {event.get('from')}: \"{subj}\" "
            f"(message {event.get('message_id')}). "
            f"Use your mail tools to check_inbox and read_message, then reply.")


async def _default_runner(cmd: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(*cmd)
    await proc.wait()


class StubWakeup:
    """Used in tests and for dry runs — records calls instead of spawning."""

    def __init__(self):
        self.calls: list[dict] = []

    async def wake(self, event: dict) -> None:
        self.calls.append(event)


class CopilotCliWakeup:
    def __init__(self, runner=_default_runner):
        self._run = runner

    async def wake(self, event: dict) -> None:
        await self._run(["copilot", "-p", _notification_text(event)])


class CopilotAppWakeup:
    def __init__(self, repo: str, runner=_default_runner):
        self.repo = repo
        self._run = runner

    async def wake(self, event: dict) -> None:
        query = urlencode({"repo": self.repo, "mode": "interactive",
                           "prompt": _notification_text(event)}, quote_via=quote)
        link = f"ghapp://session/new?{query}"
        # macOS opens custom URL schemes via `open`
        await self._run(["open", link])


class OsNotifyWakeup:
    def __init__(self, runner=_default_runner):
        self._run = runner

    async def wake(self, event: dict) -> None:
        text = _notification_text(event)
        script = f'display notification {shlex.quote(text)} with title "Courier"'
        await self._run(["osascript", "-e", script])


class TmuxWakeup:
    """Inject a notification line into the agent's tmux pane (idle interrupt)."""

    def __init__(self, pane: str, runner=_default_runner):
        self.pane = pane
        self._run = runner

    async def wake(self, event: dict) -> None:
        text = _notification_text(event)
        # -l sends the text literally (no key interpretation); Enter submits it.
        await self._run(["tmux", "send-keys", "-l", "-t", self.pane, text])
        await self._run(["tmux", "send-keys", "-t", self.pane, "Enter"])


def build_wakeup(kind: str, repo: str = "owner/repo", target: str | None = None):
    if kind == "stub":
        return StubWakeup()
    if kind == "copilot_cli":
        return CopilotCliWakeup()
    if kind == "copilot_app":
        return CopilotAppWakeup(repo=repo)
    if kind == "tmux":
        if not target:
            raise ValueError("tmux wakeup requires a target pane")
        return TmuxWakeup(pane=target)
    if kind == "os_notify":
        return OsNotifyWakeup()
    raise ValueError(f"unknown wakeup strategy: {kind}")
