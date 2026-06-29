import asyncio
import contextlib
import os
from contextlib import asynccontextmanager

import httpx
from httpx_sse import aconnect_sse
from mcp.server.fastmcp import FastMCP

from courier.listener.wakeups import build_wakeup


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


INSTRUCTIONS = (
    "You have a Courier mailbox for talking to other agents. "
    "When you see a line starting with '📬 New mail', immediately call check_inbox, "
    "then read_message and act on or reply to it. If unsure whether you have mail, "
    "call check_inbox at the start of your turn. Use set_name to pick your display name."
)


class Session:
    """Owns one agent's session: auto-register, background SSE wakeup, deregister."""

    def __init__(self, client, pane: str | None, desired_name: str | None,
                 runner=None):
        self.client = client
        self.pane = pane
        self.desired_name = desired_name
        self._runner = runner                  # injected tmux runner for tests
        self.token: str | None = None
        self.tools: MailTools | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        wakeup = {"kind": "tmux", "target": self.pane} if self.pane else {"kind": "none"}
        body = {"wakeup": wakeup}
        if self.desired_name:
            body["name"] = self.desired_name
        r = await self.client.post("/agents", json=body)
        r.raise_for_status()
        self.token = r.json()["token"]
        self.tools = MailTools(self.client, self.token)
        self._task = asyncio.create_task(self._wakeup_loop())

    def _build_wakeup(self):
        if not self.pane:
            return build_wakeup("stub")
        from courier.listener.wakeups import TmuxWakeup
        if self._runner:
            return TmuxWakeup(pane=self.pane, runner=self._runner)
        return TmuxWakeup(pane=self.pane)

    async def _wakeup_loop(self) -> None:
        import json as _json
        waker = self._build_wakeup()
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
                            await waker.wake(_json.loads(sse.data))
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self.token:
            with contextlib.suppress(Exception):
                await self.client.delete(
                    "/agents/self", headers={"Authorization": f"Bearer {self.token}"})


def build_server():
    url = os.environ.get("COURIER_URL", "http://127.0.0.1:8765")
    pane = os.environ.get("TMUX_PANE")          # inherited inside a tmux pane
    name = os.environ.get("COURIER_NAME")       # optional desired name
    client = httpx.AsyncClient(base_url=url)
    session = Session(client, pane=pane, desired_name=name)

    @asynccontextmanager
    async def lifespan(_server):
        await session.start()
        try:
            yield {"session": session}
        finally:
            await session.stop()
            await client.aclose()

    mcp = FastMCP("courier-mail", instructions=INSTRUCTIONS, lifespan=lifespan)

    @mcp.tool()
    async def list_agents() -> list[dict]:
        """List the agents currently online that you can message."""
        return await session.tools.list_agents()

    @mcp.tool()
    async def send_message(to: str, body: str, subject: str = "",
                           in_reply_to: str = "") -> dict:
        """Send a message to another agent by name."""
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
    async def set_name(name: str) -> dict:
        """Set your display name so other agents can address you by it."""
        return await session.tools.set_name(name)

    return mcp


if __name__ == "__main__":
    build_server().run()
