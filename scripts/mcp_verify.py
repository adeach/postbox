"""Verify the MCP path the way Copilot actually uses it: launch courier.mcp_server
as a real stdio MCP subprocess, do the MCP handshake, list tools, and call them.
Proves the agent-facing integration, not just the REST layer underneath.
"""
import asyncio
import os
import sys

import httpx
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from courier.api import create_app

OK = "\033[92mPASS\033[0m"


def check(label, cond):
    print(f"  [{OK if cond else 'FAIL'}] {label}")
    assert cond, f"FAILED: {label}"


def _text(result):
    # FastMCP returns tool output as TextContent; concatenate text parts.
    return "".join(getattr(c, "text", "") for c in result.content)


async def mcp_session(url, token):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "courier.mcp_server"],
        env={**os.environ, "COURIER_URL": url, "COURIER_TOKEN": token},
    )
    return params


async def main():
    app = create_app()
    cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="on")
    server = uvicorn.Server(cfg)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}"

    async with httpx.AsyncClient(base_url=url) as c:
        cop = (await c.post("/agents", json={"name": "Copilot", "address": "copilot"})).json()
        appa = (await c.post("/agents", json={"name": "App", "address": "app"})).json()

    print("\nMCP — copilot connects to courier.mcp_server over stdio")
    cop_params = await mcp_session(url, cop["token"])
    async with stdio_client(cop_params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            names = {t.name for t in tools.tools}
            check("MCP handshake + tools exposed", names >= {
                "list_agents", "send_message", "check_inbox", "read_message", "reply"})
            print(f"      tools: {sorted(names)}")

            dir_res = _text(await s.call_tool("list_agents", {}))
            check("list_agents tool returns the directory", "copilot" in dir_res and "app" in dir_res)

            send_res = _text(await s.call_tool("send_message", {
                "to": "app", "body": "review PR #42 please", "subject": "review"}))
            check("send_message tool succeeds", "review PR #42 please" in send_res)

            empty = _text(await s.call_tool("check_inbox", {"unread": True}))
            check("copilot's own inbox is empty", empty.strip() in ("[]", ""))

    print("\nMCP — app connects and reads its inbox via the tool")
    app_params = await mcp_session(url, appa["token"])
    async with stdio_client(app_params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            inbox = _text(await s.call_tool("check_inbox", {"unread": True}))
            check("app sees the message via the MCP check_inbox tool",
                  "review PR #42 please" in inbox)

    server.should_exit = True
    await task
    print("\n\033[92mMCP PATH VERIFIED\033[0m — Copilot-style stdio tools work end to end.\n")


if __name__ == "__main__":
    asyncio.run(main())
