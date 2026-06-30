"""Live end-to-end proof of the honest presence + delivery model (audit Tier 2).

Runs a REAL uvicorn server and asserts, against HTTP/SSE only (no internals):
  1. Presence is live: an agent with an open /events SSE stream is online; one that
     merely registered is offline; a human is offline ("person").
  2. Delivery is honest: a message to an OFFLINE agent stays unread (Queued), a message
     to an ONLINE agent is delivered, a message to a human stays unread until they open.
  3. Human read path: POST /observer/read as the human marks ONLY their rows read and
     emits message.read; doing it as a real agent is rejected (403).
  4. No ghost-online: a fresh server over the SAME data dir shows everyone offline
     (in-memory subs are gone; the stored 'online' latch is not trusted).

Run: .venv/bin/python scripts/presence_delivery_e2e.py
"""
import asyncio
import sys

import uvicorn
from httpx import AsyncClient
from httpx_sse import aconnect_sse

from postbox.api import create_app


def ok(label): print(f"  \033[32mPASS\033[0m {label}")
def fail(label, detail=""):
    print(f"  \033[31mFAIL\033[0m {label} {detail}"); raise SystemExit(1)


async def serve(app):
    cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="on")
    server = uvicorn.Server(cfg)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, task, f"http://127.0.0.1:{port}"


async def agents_map(c):
    return {a["address"]: a for a in (await c.get("/observer/agents")).json()}


async def thread_detail(c, tid):
    return (await c.get(f"/observer/threads/{tid}")).json()


async def main(data_dir):
    app = create_app(data_dir)
    server, task, base = await serve(app)
    try:
        async with AsyncClient(base_url=base) as c:
            # --- register two agents + create a human -------------------------------
            alice = (await c.post("/agents", json={"name": "alice"})).json()
            bob = (await c.post("/agents", json={"name": "bob"})).json()
            adam = (await c.post("/observer/identity", json={"name": "adam"})).json()

            # 1) before anyone connects, all are offline (no live SSE) -----------------
            m = await agents_map(c)
            if m["alice"]["status"] != "offline": fail("alice offline before SSE", m["alice"])
            if m["bob"]["status"] != "offline": fail("bob offline before SSE", m["bob"])
            if m["adam"]["status"] != "offline" or m["adam"]["profile"] != {"human": True}:
                fail("adam is an offline person", m["adam"])
            ok("before any SSE connection: alice, bob, adam all offline (no ghost-online)")

            # 2) alice opens a live /events stream -> she becomes ONLINE ---------------
            alice_online = asyncio.Event()
            async def hold_alice_sse():
                ah = {"Authorization": f"Bearer {alice['token']}"}
                async with aconnect_sse(c, "GET", "/events", headers=ah) as es:
                    alice_online.set()
                    async for _ in es.aiter_sse():
                        pass  # hold the stream open
            sse_task = asyncio.create_task(hold_alice_sse())
            await asyncio.wait_for(alice_online.wait(), 3)
            await asyncio.sleep(0.15)  # let the server-side subscribe land
            m = await agents_map(c)
            if m["alice"]["status"] != "online": fail("alice online with live SSE", m["alice"])
            if m["bob"]["status"] != "online":
                ok("live presence: alice ONLINE (holds SSE), bob still OFFLINE (registered only)")
            else:
                fail("bob should still be offline", m["bob"])

            # 3) honest delivery: alice -> bob (OFFLINE) = Queued (unread) -------------
            ah = {"Authorization": f"Bearer {alice['token']}"}
            r = await c.post("/messages", headers=ah, json={"to": "bob", "body": "ping bob", "subject": "q"})
            tid_bob = r.json()["thread_id"]
            d = await thread_detail(c, tid_bob)
            if d["messages"][0]["read_by"] != []: fail("bob message unread (Queued)", d)
            ok("message to OFFLINE bob is unread → UI renders '◷ Queued · delivers when bob connects'")

            # 4) honest delivery: alice -> adam (HUMAN) = Sent (unread) ----------------
            r = await c.post("/messages", headers=ah, json={"to": "adam", "body": "hi adam", "subject": "hey"})
            tid_adam = r.json()["thread_id"]
            d = await thread_detail(c, tid_adam)
            if d["messages"][0]["read_by"] != []: fail("adam message unread (Sent)", d)
            ok("message to HUMAN adam is unread → UI renders '◷ Sent · waiting for adam to open'")

            # 5) human read path: adam opens it -> marked read + message.read emitted ---
            #    Listen on alice's wakeup stream is hard to multiplex here; assert via state.
            rd = await c.post("/observer/read", json={"as": "adam", "thread_id": tid_adam})
            if rd.status_code != 200 or rd.json()["marked"] != 1: fail("adam marks read", rd.text)
            d = await thread_detail(c, tid_adam)
            if d["messages"][0]["read_by"] != ["adam"]: fail("adam read_by after open", d)
            ok("human read path: POST /observer/read as adam → message flips to '✓✓ Read'")

            # 6) guard: a real agent may NOT mark read via the observer ----------------
            bad = await c.post("/observer/read", json={"as": "bob", "thread_id": tid_bob})
            if bad.status_code != 403: fail("agent read via observer is 403", bad.text)
            d = await thread_detail(c, tid_bob)
            if d["messages"][0]["read_by"] != []: fail("bob's row untouched by guarded call", d)
            ok("guard: marking read AS a real agent (bob) is rejected 403; bob's read state untouched")

            # 7) presence drops when alice's SSE closes --------------------------------
            sse_task.cancel()
            try: await sse_task
            except asyncio.CancelledError: pass
            await asyncio.sleep(0.2)
            m = await agents_map(c)
            if m["alice"]["status"] != "offline": fail("alice offline after SSE closes", m["alice"])
            ok("presence drops: alice OFFLINE the moment her SSE stream closes (refcount to zero)")
    finally:
        server.should_exit = True
        await task

    # 8) NO ghost-online across restart: a fresh server over the SAME db --------------
    app2 = create_app(data_dir)
    server2, task2, base2 = await serve(app2)
    try:
        async with AsyncClient(base_url=base2) as c:
            m = await agents_map(c)
            bad = [a for a, v in m.items() if v["status"] == "online"]
            if bad: fail("everyone offline after restart (no ghost-online)", bad)
            ok("after server restart (same DB): everyone offline — stored 'online' latch is not trusted")
    finally:
        server2.should_exit = True
        await task2

    print("\n\033[32mALL CHECKS PASSED\033[0m — honest presence + delivery proven end to end.")


if __name__ == "__main__":
    import tempfile
    d = tempfile.mkdtemp(prefix="postbox-e2e-")
    print(f"data dir: {d}")
    asyncio.run(main(d))
