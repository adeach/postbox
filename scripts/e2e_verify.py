"""End-to-end verification of Postbox against everything we designed.

Runs the REAL service (uvicorn), the REAL MCP MailTools client, and the REAL
listener daemon over SSE — black-box over HTTP — and asserts each promised
capability. Not committed as a test; this is a live proof harness.
"""
import asyncio
import json
import logging

import httpx
import uvicorn
from httpx_sse import aconnect_sse

from postbox.api import create_app
from postbox.mcp_server import MailTools
from postbox.listener.daemon import run_daemon
from postbox.listener.wakeups import StubWakeup

OK = "\033[92mPASS\033[0m"


def check(label, cond):
    print(f"  [{OK if cond else 'FAIL'}] {label}")
    assert cond, f"FAILED: {label}"


async def collect_sse(client, token, last_event_id, want, timeout=3.0):
    """Open an SSE stream and collect up to `want` events, then return."""
    out = []
    headers = {"Authorization": f"Bearer {token}", "Last-Event-ID": str(last_event_id)}

    async def _run():
        async with aconnect_sse(client, "GET", "/events", headers=headers) as es:
            async for sse in es.aiter_sse():
                out.append((int(sse.id), sse.event, json.loads(sse.data)))
                if len(out) >= want:
                    return
    try:
        await asyncio.wait_for(_run(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    return out


async def main():
    app = create_app()
    cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="on")
    server = uvicorn.Server(cfg)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    async with httpx.AsyncClient(base_url=base) as c:
        print("\n1. IDENTITY — agents register and get tokens")
        cop = (await c.post("/agents", json={"name": "Copilot CLI", "address": "copilot"})).json()
        appa = (await c.post("/agents", json={"name": "Copilot App", "address": "app"})).json()
        claude = (await c.post("/agents", json={"name": "Claude", "address": "claude"})).json()
        check("copilot got a token", bool(cop["token"]))
        check("app got a distinct token", appa["token"] != cop["token"])

        print("\n2. DIRECTORY — discovery lists all agents, leaks no tokens")
        directory = (await c.get("/agents")).json()
        addrs = {a["address"] for a in directory}
        check("directory has copilot/app/claude", {"copilot", "app", "claude"} <= addrs)
        check("directory entries carry no token", all("token" not in a for a in directory))

        print("\n3. AUTH ISOLATION — token required; can't act as someone else")
        r = await c.post("/messages", json={"to": "app", "body": "x"})  # no token
        check("unauthenticated send rejected (401)", r.status_code == 401)

        # MCP tool clients (the path Copilot CLI / app actually use)
        cop_mcp = MailTools(c, cop["token"])
        app_mcp = MailTools(c, appa["token"])

        print("\n4. NOTIFICATION — recipient's listener is woken on arrival (SSE→daemon)")
        woken = StubWakeup()
        daemon = asyncio.create_task(run_daemon(base, appa["token"], woken))
        await asyncio.sleep(0.3)  # let the daemon establish its SSE stream
        sent = await cop_mcp.send_message(to="app", body="can you review PR #42?", subject="review")
        await asyncio.sleep(0.4)
        check("listener fired exactly one wakeup", len(woken.calls) == 1)
        check("wakeup names the sender", woken.calls and woken.calls[0]["from"] == "copilot")
        check("wakeup matches the delivered message",
              woken.calls[0]["message_id"] == sent["id"])

        print("\n5. DURABLE INBOX — recipient reads mail via REST (source of truth)")
        inbox = await app_mcp.check_inbox(unread=True)
        check("app has 1 unread message", len(inbox) == 1)
        check("body is intact", inbox[0]["body"] == "can you review PR #42?")

        print("\n6. READ + RECEIPT — reading marks read and notifies the sender")
        await app_mcp.read_message(sent["id"])
        check("now 0 unread for app", len(await app_mcp.check_inbox(unread=True)) == 0)
        receipts = await collect_sse(c, cop["token"], 0, want=1)  # copilot's event stream
        check("sender received a message.read receipt",
              any(ev == "message.read" for _, ev, _ in receipts))

        print("\n7. THREADING — reply stays in the same thread and routes back")
        reply = await app_mcp.reply(message_id=sent["id"], body="sure, looking now")
        check("reply shares the original thread_id", reply["thread_id"] == sent["thread_id"])
        cop_inbox = await cop_mcp.check_inbox(unread=True)
        check("copilot received the reply", any(m["body"] == "sure, looking now" for m in cop_inbox))
        thread = (await c.get(f"/threads/{sent['thread_id']}",
                              headers={"Authorization": f"Bearer {cop['token']}"})).json()
        check("thread holds both messages in order",
              [m["body"] for m in thread] == ["can you review PR #42?", "sure, looking now"])

        print("\n8. OFFLINE DURABILITY — agent with NO listener still gets mail")
        await cop_mcp.send_message(to="claude", body="ping while you were away", subject="async")
        claude_mcp = MailTools(c, claude["token"])
        claude_inbox = await claude_mcp.check_inbox(unread=True)
        check("claude (never connected to SSE) sees the message later",
              [m["body"] for m in claude_inbox] == ["ping while you were away"])

        print("\n9. SSE REPLAY — reconnect with Last-Event-ID replays missed events")
        replayed = await collect_sse(c, claude["token"], 0, want=1)
        check("claude replays the event missed while offline",
              len(replayed) == 1 and replayed[0][1] == "message.received")

        daemon.cancel()

    server.should_exit = True
    await task
    print("\n\033[92mALL END-TO-END CHECKS PASSED\033[0m — every capability we designed is proven live.\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
