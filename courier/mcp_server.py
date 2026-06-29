import os

import httpx
from mcp.server.fastmcp import FastMCP


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


def build_server() -> FastMCP:
    url = os.environ.get("COURIER_URL", "http://127.0.0.1:8765")
    token = os.environ["COURIER_TOKEN"]
    client = httpx.AsyncClient(base_url=url)
    tools = MailTools(client, token)

    mcp = FastMCP("courier-mail")

    @mcp.tool()
    async def list_agents() -> list[dict]:
        """List all agents you can message (the directory)."""
        return await tools.list_agents()

    @mcp.tool()
    async def send_message(to: str, body: str, subject: str = "",
                           in_reply_to: str = "") -> dict:
        """Send a message to another agent by address."""
        return await tools.send_message(
            to=to, body=body, subject=subject or None,
            in_reply_to=in_reply_to or None,
        )

    @mcp.tool()
    async def check_inbox(unread: bool = True) -> list[dict]:
        """List messages in your inbox. unread=True shows only unread."""
        return await tools.check_inbox(unread=unread)

    @mcp.tool()
    async def read_message(message_id: str) -> dict:
        """Read a message by id (marks it read)."""
        return await tools.read_message(message_id)

    @mcp.tool()
    async def reply(message_id: str, body: str) -> dict:
        """Reply to a message, keeping it in the same thread."""
        return await tools.reply(message_id, body)

    return mcp


if __name__ == "__main__":
    build_server().run()
