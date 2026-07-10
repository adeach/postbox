import asyncio
import os
import shlex
import time
from urllib.parse import quote, urlencode

_WAKE_LOG = os.environ.get("POSTBOX_WAKEUP_LOG", "/tmp/postbox_wakeup.log")


def _log(msg: str) -> None:
    try:
        with open(_WAKE_LOG, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _notification_text(event: dict) -> str:
    subj = event.get("subject") or "(no subject)"
    return (f"📬 New mail from {event.get('from')}: \"{subj}\" "
            f"(message {event.get('message_id')}). "
            f"Use your mail tools to check_inbox and read_message, then reply.")


def _is_busy(pane: str) -> bool:
    """True if Copilot is mid-turn (still processing). Its status line shows an
    'esc interrupt' hint while a turn runs; when idle that hint is gone and the
    line reads '/ commands · ? help'. Injecting a wakeup while busy leaves the
    notification text sitting as '[pending]' and it never submits — silently
    dropping the wake. So we wait for this to clear before poking."""
    return "esc interrupt" in pane


def _input_box_content(pane: str) -> str:
    """Extract the text currently in the agent's input box from a captured pane.

    Copilot draws a bordered box: a top border line starting with '╻', content
    line(s), then a bottom border starting with '╹'. We return the content
    between the LAST such top border and its bottom border. Falls back to the
    last line containing the simple '❯' prompt glyph."""
    lines = pane.splitlines()
    tops = [i for i, l in enumerate(lines) if l.lstrip().startswith("╻")]
    if tops:
        content = []
        for l in lines[tops[-1] + 1:]:
            if l.lstrip().startswith("╹"):
                break
            content.append(l)
        return " ".join(content)
    prompts = [l for l in lines if "❯" in l]
    return prompts[-1] if prompts else ""


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
                 poll_interval: float = 0.6, idle_timeout: float = 240.0):
        self.pane = pane
        self._run = runner
        self._enter_delay = enter_delay
        self._capture = capturer
        self._max_attempts = max_submit_attempts
        self._poll = poll_interval
        self._idle_timeout = idle_timeout

    async def wake(self, event: dict) -> None:
        text = _notification_text(event)
        # A stable substring of the typed text, used to detect it still sitting in
        # the input box. message_id is unique; fall back to a fixed phrase.
        marker = str(event.get("message_id") or "") or "New mail from"
        _log(f"WAKE pane={self.pane!r} marker={marker[:12]} delay={self._enter_delay}")
        # CRITICAL: only poke when the agent is IDLE. Typing into a busy (mid-turn)
        # Copilot TUI leaves the notification as '[pending]' and it never submits, so
        # the wake is silently lost and the agent stalls with unread mail. Wait first.
        await self._wait_until_idle()
        # -l sends the text literally (no key interpretation).
        await self._run(["tmux", "send-keys", "-l", "-t", self.pane, text])
        _log("  typed text")
        if self._enter_delay:
            await asyncio.sleep(self._enter_delay)
        for attempt in range(1, self._max_attempts + 1):
            # Copilot (an Ink TUI) only submits Enter when it believes it is
            # focused. When the agent's window isn't the one you're looking at,
            # it ignores the injected Enter. Sending a synthetic focus-in report
            # (ESC [ I) right before Enter makes it accept the submit WITHOUT
            # stealing your view. Verified on Ghostty + tmux.
            await self._run(["tmux", "send-keys", "-t", self.pane, "-H",
                             "1b", "5b", "49"])     # ESC [ I  = focus-in
            await asyncio.sleep(0.15)
            await self._run(["tmux", "send-keys", "-t", self.pane, "Enter"])
            await asyncio.sleep(self._poll)
            still = await self._still_in_input(marker)
            _log(f"  focus+enter#{attempt} -> still_in_input={still}")
            if not still:
                _log("  SUBMITTED (input cleared)")
                return
        _log("  GAVE UP after max attempts (text still in input box)")

    async def _wait_until_idle(self) -> None:
        """Block until the agent's pane is idle (no 'esc interrupt' hint), so the
        wakeup lands on a TUI that will actually accept + submit it. Bounded by
        idle_timeout; if the turn runs longer we poke anyway as a last resort
        (better a possibly-pending poke than none)."""
        deadline = time.monotonic() + self._idle_timeout
        waited = False
        while time.monotonic() < deadline:
            try:
                pane = await self._capture(self.pane)
            except Exception as e:
                _log(f"  idle-check capture failed: {e!r}")
                return          # can't read → don't block the wake forever
            if not _is_busy(pane):
                if waited:
                    _log("  agent went idle → poking now")
                return
            waited = True
            await asyncio.sleep(self._poll)
        _log("  idle wait TIMED OUT (still busy) → poking anyway")

    async def _still_in_input(self, marker: str) -> bool:
        """True if our text is still sitting in the pane's input box (not submitted).

        Handles both Copilot's bordered box (╻▄..┃..╹▀) and the simple ❯ prompt:
        the input *content* is what's between the last top-border and bottom-border,
        or the last ❯ line. After submit, that content no longer holds the marker."""
        try:
            pane = await self._capture(self.pane)
        except Exception as e:
            _log(f"  capture FAILED: {e!r}")
            return False  # can't read the pane → stop retrying rather than spam
        content = _input_box_content(pane)
        _log(f"  input-box={content.strip()[:90]!r}")
        return marker in content


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
