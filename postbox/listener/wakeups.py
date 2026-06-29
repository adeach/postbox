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


async def _default_capturer(pane: str) -> str:
    """Return the visible text of a tmux pane (used to confirm a poke submitted)."""
    proc = await asyncio.create_subprocess_exec(
        "tmux", "capture-pane", "-p", "-t", pane,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out.decode(errors="replace")


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
        script = f'display notification {shlex.quote(text)} with title "Postbox"'
        await self._run(["osascript", "-e", script])


class TmuxWakeup:
    """Inject a notification line into the agent's tmux pane and SUBMIT it.

    Submitting is the unreliable part: Copilot/Claude are Ink TUIs, and a single
    injected Enter doesn't always register (paste batching, transitional render
    state right after a turn). So we type the text, then press Enter and *verify*
    the input box actually cleared by reading the pane back — retrying Enter until
    it does. This makes the wakeup reliable regardless of the agent's exact state.
    """

    def __init__(self, pane: str, runner=_default_runner, enter_delay: float = 0.4,
                 capturer=_default_capturer, max_submit_attempts: int = 6,
                 poll_interval: float = 0.6):
        self.pane = pane
        self._run = runner
        self._enter_delay = enter_delay
        self._capture = capturer
        self._max_attempts = max_submit_attempts
        self._poll = poll_interval

    async def wake(self, event: dict) -> None:
        text = _notification_text(event)
        # A stable substring of the typed text, used to detect it still sitting in
        # the input box. message_id is unique; fall back to a fixed phrase.
        marker = str(event.get("message_id") or "") or "New mail from"
        # -l sends the text literally (no key interpretation).
        await self._run(["tmux", "send-keys", "-l", "-t", self.pane, text])
        if self._enter_delay:
            await asyncio.sleep(self._enter_delay)
        for _ in range(self._max_attempts):
            await self._run(["tmux", "send-keys", "-t", self.pane, "Enter"])
            await asyncio.sleep(self._poll)
            if not await self._still_in_input(marker):
                return  # submitted (input box cleared)
        # Could not confirm submission; the message is still durably in the inbox.

    async def _still_in_input(self, marker: str) -> bool:
        """True if the pane's input prompt (last line containing the prompt glyph)
        still holds our text — i.e. it has not been submitted yet."""
        try:
            pane = await self._capture(self.pane)
        except Exception:
            return False  # can't read the pane → stop retrying rather than spam
        prompt_lines = [ln for ln in pane.splitlines() if "❯" in ln]  # ❯
        if not prompt_lines:
            return False
        return marker in prompt_lines[-1]


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
