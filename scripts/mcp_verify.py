"""Verify the v2 MCP path the way Copilot actually uses it: launch postbox.mcp_server
as a real stdio MCP subprocess with ONLY POSTBOX_URL set (token-LESS config), do the
MCP handshake, list tools, and confirm the server auto-registered its own identity.

v2 changed the MCP surface: the server no longer reads POSTBOX_TOKEN — its lifespan
auto-registers a session identity (a `copilot-*` agent) on startup, exposes a `set_name`
tool, and deregisters on shutdown. So we pass no token, ensure TMUX_PANE is absent
(wakeup kind 'none'), and assert the new behaviour over real stdio MCP.
"""
import asyncio
import os
import sys
import tempfile

import httpx
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from postbox.api import create_app

OK = "\033[92mPASS\033[0m"


def check(label, cond):
    print(f"  [{OK if cond else 'FAIL'}] {label}")
    assert cond, f"FAILED: {label}"


def _text(result):
    # FastMCP returns tool output as TextContent; concatenate text parts.
    return "".join(getattr(c, "text", "") for c in result.content)


def mcp_params(url):
    # Token-LESS v2 config: only POSTBOX_URL. Strip POSTBOX_TOKEN (ignored now) and
    # TMUX_PANE (so the auto-registered session uses wakeup kind 'none', not tmux —
    # otherwise a shell running inside tmux would leak its real pane in).
    env = {**os.environ, "POSTBOX_URL": url}
    env.pop("POSTBOX_TOKEN", None)
    env.pop("TMUX_PANE", None)
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "postbox.mcp_server"],
        env=env,
    )


async def main():
    app = create_app(tempfile.mkdtemp())
    cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="on")
    server = uvicorn.Server(cfg)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}"

    print("\nMCP v2 — a Copilot session connects to postbox.mcp_server over stdio (no token)")
    async with stdio_client(mcp_params(url)) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            names = {t.name for t in tools.tools}
            check("MCP handshake + v2 tools exposed (incl. set_name)", names >= {
                "list_agents", "send_message", "check_inbox", "read_message",
                "reply", "set_name"})
            print(f"      tools: {sorted(names)}")

            # The server auto-registered its own identity on startup — the directory
            # (online-only) must show a `copilot-*` agent. Check it WHILE the
            # subprocess is alive; on shutdown the session deregisters (goes offline).
            dir_res = _text(await s.call_tool("list_agents", {}))
            check("server auto-registered a copilot-* session identity",
                  "copilot-" in dir_res)
            print(f"      directory: {dir_res}")

    # After the stdio session closes, its lifespan deregistered the agent: the online
    # directory is empty again (proves clean deregister).
    async with httpx.AsyncClient(base_url=url) as c:
        online = (await c.get("/agents")).json()
        check("session deregistered on shutdown (online directory empty)", online == [])

    server.should_exit = True
    await task
    print("\n\033[92mMCP v2 PATH VERIFIED\033[0m — token-less stdio session auto-registers, "
          "exposes set_name, deregisters on exit.\n")


if __name__ == "__main__":
    asyncio.run(main())
