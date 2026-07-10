import asyncio
import contextlib
import os
from contextlib import asynccontextmanager

import httpx
from httpx_sse import aconnect_sse
from mcp.server.fastmcp import FastMCP

from postbox.listener.wakeups import build_wakeup


class MailTools:
    """Thin REST client used by the MCP tools (and unit tests)."""

    def __init__(self, client: httpx.AsyncClient, token: str):
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}"}

    async def list_agents(self) -> list[dict]:
        r = await self.client.get("/agents")
        r.raise_for_status()
        return r.json()

    async def send_message(self, to: str, body: str, subject: str | None = None,
                           content_type: str = "text/plain",
                           in_reply_to: str | None = None) -> dict:
        r = await self.client.post("/messages", headers=self.headers, json={
            "to": to, "body": body, "subject": subject,
            "content_type": content_type, "in_reply_to": in_reply_to,
        })
        r.raise_for_status()
        return r.json()

    async def check_inbox(self, unread: bool = True, thread: str | None = None) -> list[dict]:
        params = {"unread": unread}
        if thread:
            params["thread"] = thread
        r = await self.client.get("/inbox", headers=self.headers, params=params)
        r.raise_for_status()
        return r.json()

    async def read_message(self, message_id: str) -> dict:
        r = await self.client.get(f"/messages/{message_id}", headers=self.headers)
        r.raise_for_status()
        return r.json()

    async def reply(self, message_id: str, body: str,
                    content_type: str = "text/plain") -> dict:
        # fetch the original to find sender + thread, then send a reply to the sender
        original = await self.read_message(message_id)
        return await self.send_message(
            to=original["sender"], body=body, content_type=content_type,
            in_reply_to=message_id,
        )

    async def set_name(self, name: str) -> dict:
        r = await self.client.patch("/agents/self", headers=self.headers,
                                    json={"name": name})
        r.raise_for_status()
        return r.json()

    async def spawn_terminal(self, name: str, cwd: str | None = None,
                             instance: str | None = None,
                             model: str | None = None,
                             project: str | None = None) -> dict:
        r = await self.client.post("/spawn", headers=self.headers, json={
            "name": name, "cwd": cwd, "instance": instance, "model": model,
            "project": project})
        r.raise_for_status()
        return r.json()


INSTRUCTIONS = (
    "Postbox is your mailbox for collaborating with other AI agents.\n"
    "• MAIL: when you see a line starting with '📬 New mail', immediately call check_inbox, "
    "then read_message, then act on it and reply. If unsure, call check_inbox at the start of "
    "your turn. Always reply with the outcome so the sender knows you're done — don't go idle "
    "silently.\n"
    "• ADDRESSING: message a local agent by its name; message an agent on a peer postbox as "
    "name@instance (e.g. 'reviewer@vm'). Call list_agents to see who exists.\n"
    "• DELEGATING: use spawn_terminal to spin up ANOTHER copilot agent you can then talk to — "
    "locally or on a peer (instance='vm'), optionally with its own model. This lets you build a "
    "team (e.g. one agent per role: frontend, backend, reviewer, tester). Pass the SAME "
    "project='<task>' for every teammate so the whole team shares one tmux session. Wait until "
    "the result says registered=true, then message it by name. When YOU are a spawned worker, "
    "report your progress to whoever tasked you and ask them (via send_message) when you need "
    "input.\n"
    "• Use set_name to pick your display name."
)


class Session:
    """Owns one agent's session: auto-register, background SSE wakeup, deregister.

    If a token is provided (POSTBOX_TOKEN — fleet mode), the session acts AS that
    pre-registered durable identity instead of auto-registering, and does NOT
    deregister on exit (the identity outlives the turn)."""

    def __init__(self, client, pane: str | None, desired_name: str | None,
                 runner=None, token: str | None = None, session_key: str | None = None):
        self.client = client
        self.pane = pane
        self.desired_name = desired_name
        self._runner = runner                  # injected tmux runner for tests
        self.token: str | None = token
        self._durable = token is not None      # provided token → don't register/deregister
        self.session_key = session_key         # COPILOT_AGENT_SESSION_ID → resumable identity
        self.tools: MailTools | None = MailTools(client, token) if token else None
        self._tasks: list[asyncio.Task] = []
        self._waker = None
        self._poke_lock: asyncio.Lock | None = None

    async def start(self) -> None:
        if not self._durable:
            wakeup = {"kind": "tmux", "target": self.pane} if self.pane else {"kind": "none"}
            body = {"wakeup": wakeup}
            if self.desired_name:
                body["name"] = self.desired_name
            if self.session_key:
                body["session_key"] = self.session_key
            r = await self.client.post("/agents", json=body)
            r.raise_for_status()
            self.token = r.json()["token"]
            self.tools = MailTools(self.client, self.token)
        self._waker = self._build_wakeup()
        self._poke_lock = asyncio.Lock()
        self._tasks = [asyncio.create_task(self._wakeup_loop())]
        # defense-in-depth watchdog: a periodic self-check that re-pokes if mail stays
        # unread (catches any wake the SSE path ever drops). Only for tmux-poked agents.
        if self.pane and float(os.environ.get("POSTBOX_SAFETY_POLL", "45")) > 0:
            self._tasks.append(asyncio.create_task(self._safety_loop()))

    async def _poke(self, event: dict) -> None:
        # serialize pokes so the SSE loop and the watchdog never type into the pane at once
        async with self._poke_lock:
            await self._waker.wake(event)

    def _build_wakeup(self):
        if not self.pane:
            return build_wakeup("stub")
        from postbox.listener.wakeups import TmuxWakeup
        delay = float(os.environ.get("POSTBOX_ENTER_DELAY", "0.4"))  # tune if Enter doesn't submit
        if self._runner:
            return TmuxWakeup(pane=self.pane, runner=self._runner, enter_delay=delay)
        return TmuxWakeup(pane=self.pane, enter_delay=delay)

    async def _wakeup_loop(self) -> None:
        import json as _json
        # Track the last seen event id so a reconnect resumes from there instead
        # of replaying (and re-poking) the whole history. A fresh session starts
        # at 0 (empty inbox), so no spurious pokes on first connect either.
        last_id = "0"
        while True:
            try:
                headers = {"Authorization": f"Bearer {self.token}",
                           "Last-Event-ID": last_id}
                async with aconnect_sse(self.client, "GET", "/events",
                                        headers=headers) as es:
                    async for sse in es.aiter_sse():
                        last_id = sse.id or last_id
                        if sse.event == "message.received":
                            await self._poke(_json.loads(sse.data))
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1)

    async def _safety_loop(self) -> None:
        """Watchdog: if mail stays unread across a full poll interval, the SSE wake was
        dropped (reconnect gap, missed event, …) — re-poke. Requiring the message to
        persist one interval means the normal SSE wake handles fresh mail first, so this
        only fires on genuinely stuck mail, not every new message."""
        interval = float(os.environ.get("POSTBOX_SAFETY_POLL", "45"))
        prev_unread: set[str] = set()
        while True:
            await asyncio.sleep(interval)
            try:
                r = await self.client.get(
                    "/inbox", params={"unread": "true"},
                    headers={"Authorization": f"Bearer {self.token}"})
                msgs = r.json() if r.status_code == 200 else []
                by_id = {m["id"]: m for m in msgs}
                stuck = set(by_id) & prev_unread          # unread for a whole interval
                if stuck:
                    m = by_id[next(iter(stuck))]
                    await self._poke({"from": m.get("sender") or m.get("from"),
                                      "subject": m.get("subject"),
                                      "message_id": m["id"]})
                prev_unread = set(by_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        # A resumable identity (has a session_key) must PERSIST on exit so it stays
        # listed (offline) and can be resumed later — presence drops automatically when
        # the SSE connection closes. Only a non-resumable ephemeral session is deregistered.
        if self.token and not self._durable and not self.session_key:
            with contextlib.suppress(Exception):
                await self.client.delete(
                    "/agents/self", headers={"Authorization": f"Bearer {self.token}"})


def build_server():
    url = os.environ.get("POSTBOX_URL", "http://127.0.0.1:8765")
    token = os.environ.get("POSTBOX_TOKEN")     # fleet mode: act as this durable identity
    pane = os.environ.get("TMUX_PANE")          # inherited inside a tmux pane
    name = os.environ.get("POSTBOX_NAME")       # optional desired name
    session_key = os.environ.get("COPILOT_AGENT_SESSION_ID")  # resume → same identity
    client = httpx.AsyncClient(base_url=url)
    # A durable/fleet identity (token provided) is headless spawn-on-arrival — it must
    # never set up a tmux wakeup on an inherited pane, so ignore TMUX_PANE in that mode.
    session = Session(client, pane=(None if token else pane), desired_name=name,
                      token=token, session_key=(None if token else session_key))

    @asynccontextmanager
    async def lifespan(_server):
        try:
            await session.start()       # inside try so the client is closed even if boot fails
            yield {"session": session}
        finally:
            await session.stop()
            await client.aclose()

    mcp = FastMCP("postbox-mail", instructions=INSTRUCTIONS, lifespan=lifespan)

    @mcp.tool()
    async def list_agents() -> list[dict]:
        """List agents you can message. Includes offline ones — mail to an offline agent
        is delivered when it reconnects. Remote agents appear as name@instance."""
        return await session.tools.list_agents()

    @mcp.tool()
    async def send_message(to: str, body: str, subject: str = "",
                           in_reply_to: str = "") -> dict:
        """Send a message to another agent. `to` is its name for a local agent, or
        name@instance for an agent on a peer postbox (e.g. 'reviewer@vm')."""
        return await session.tools.send_message(
            to=to, body=body, subject=subject or None, in_reply_to=in_reply_to or None)

    @mcp.tool()
    async def check_inbox(unread: bool = True) -> list[dict]:
        """List messages in your inbox (unread=True shows only unread)."""
        return await session.tools.check_inbox(unread=unread)

    @mcp.tool()
    async def read_message(message_id: str) -> dict:
        """Read a message by id (marks it read)."""
        return await session.tools.read_message(message_id)

    @mcp.tool()
    async def reply(message_id: str, body: str) -> dict:
        """Reply to a message, keeping it in the same thread."""
        return await session.tools.reply(message_id, body)

    @mcp.tool()
    async def spawn_terminal(name: str, cwd: str = "", instance: str = "",
                             model: str = "", project: str = "") -> dict:
        """Spin up a NEW interactive copilot agent (a window in a tmux session) that you
        can then talk to. `project`: groups a team into ONE tmux session `postbox_<project>`
        (one window per agent) — pass the SAME project for every teammate on a task so they
        live together (attach the whole team with `tmux attach -t postbox_<project>`);
        empty = the shared 'main' session. `instance`: empty = spawn locally, or a peer name
        (e.g. "vm") to spawn on that peer (then message it at name@instance). `model`: set a
        specific model (e.g. "claude-opus-4.8") — pass your OWN model to have it inherit
        yours; empty = default. Returns {name, session, project, window, attach, registered,
        address?}: message it once `registered` is true."""
        return await session.tools.spawn_terminal(
            name, cwd or None, instance or None, model or None, project or None)

    # A durable/fleet identity (token provided) must NOT rename itself — its address
    # is a fixed, referenced key. Only expose set_name for self-registering sessions.
    if not token:
        @mcp.tool()
        async def set_name(name: str) -> dict:
            """Set your display name so other agents can address you by it."""
            return await session.tools.set_name(name)

    return mcp


if __name__ == "__main__":
    build_server().run()
