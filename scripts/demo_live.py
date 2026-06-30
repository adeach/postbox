"""Live demo: real server + seeded scenario so you can click the real Observatory.

Starts uvicorn on http://127.0.0.1:8765 with a fresh DB, then seeds:
  - alice  : an agent, held ONLINE via a live /events SSE stream
  - bob    : an agent, registered but OFFLINE (no live session)
  - adam   : a human ("person")
and messages exercising every receipt state:
  - alice -> bob  "thanks, looking"     (bob reads it)        -> ✓✓ Read
  - alice -> bob  "ping when you pick up"(bob offline, unread)-> ◷ Queued
  - alice -> adam "can you review?"      (human, unread)      -> ◷ Sent
  - bob   -> alice "staging build pushed"(alice online)       -> ✓ Delivered

Then open:  http://127.0.0.1:8765/ui/?as=alice   (alice's outbound receipts)
       or:  http://127.0.0.1:8765/ui/?as=adam    (open as the human -> auto Read)
Ctrl-C to stop.
"""
import asyncio

import uvicorn
from httpx import AsyncClient
from httpx_sse import aconnect_sse

from postbox.api import create_app

PORT = 8765


async def main(data_dir):
    app = create_app(data_dir)
    cfg = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", lifespan="on")
    server = uvicorn.Server(cfg)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    base = f"http://127.0.0.1:{PORT}"

    async with AsyncClient(base_url=base, timeout=None) as c:  # no read-timeout: hold SSE open
        alice = (await c.post("/agents", json={"name": "alice"})).json()
        bob = (await c.post("/agents", json={"name": "bob"})).json()
        await c.post("/observer/identity", json={"name": "adam"})
        ah = {"Authorization": f"Bearer {alice['token']}"}
        bh = {"Authorization": f"Bearer {bob['token']}"}

        # hold alice ONLINE with a live SSE stream
        ready = asyncio.Event()
        async def hold():
            async with aconnect_sse(c, "GET", "/events", headers=ah) as es:
                ready.set()
                async for _ in es.aiter_sse():
                    pass
        hold_task = asyncio.create_task(hold())
        await asyncio.wait_for(ready.wait(), 3)
        await asyncio.sleep(0.15)

        # seed messages
        m_read = (await c.post("/messages", headers=ah, json={"to": "bob", "body": "thanks, looking", "subject": "deploy plan"})).json()
        await c.get(f"/messages/{m_read['id']}", headers=bh)                    # bob reads -> ✓✓ Read
        await c.post("/messages", headers=ah, json={"to": "bob", "body": "ping when you pick this up", "in_reply_to": m_read["id"]})  # -> Queued
        await c.post("/messages", headers=ah, json={"to": "adam", "body": "can you review the deploy plan?", "subject": "review?"})    # -> Sent
        await c.post("/messages", headers=bh, json={"to": "alice", "body": "staging build pushed, take a look", "subject": "deploy plan"})  # -> Delivered (alice online)

        print(f"\n  Seeded. Open:  {base}/ui/?as=alice   (alice's outbound: Read / Queued / Sent / Delivered)")
        print(f"           or:  {base}/ui/?as=adam    (open AS the human -> watch it flip to Read)\n")
        try:
            await hold_task
        except asyncio.CancelledError:
            pass
    await task


if __name__ == "__main__":
    import tempfile
    asyncio.run(main(tempfile.mkdtemp(prefix="postbox-demo-")))
