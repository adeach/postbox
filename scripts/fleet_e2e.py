"""Live end-to-end proof of Fleet mode (real uvicorn + a real spawned subprocess).

Proves the parts unit tests can't: the Supervisor actually spawns a headless
process, that process authenticates AS its durable fleet identity via the injected
POSTBOX_TOKEN, drains its inbox and replies — and multiple messages are coalesced
into ONE turn.

The "agent" here is a tiny stdlib (urllib) script standing in for `copilot -p`.
Determinism: the fleet agent is added DISABLED, all mail is sent while it can't
spawn, then it's ENABLED — so exactly one turn runs and must see all the mail.

Run:  python -m scripts.fleet_e2e   (from the repo root)
"""
import asyncio
import json
import os
import socket
import sys
import textwrap

import httpx
import uvicorn

# The stand-in headless agent: read unread mail (marking each read), send one
# per-turn marker, then reply to each. One marker per turn ⇒ coalescing is visible.
FAKE_AGENT = textwrap.dedent("""
    import os, json, urllib.request
    url = os.environ["POSTBOX_URL"]; tok = os.environ["POSTBOX_TOKEN"]
    def req(method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url + path, data=data, method=method,
            headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
        with urllib.request.urlopen(r) as resp:
            return json.loads(resp.read() or "null")
    inbox = req("GET", "/inbox?unread=true")
    if inbox:
        req("POST", "/messages", {"to": inbox[0]["sender"],
                                  "subject": "turn", "body": "drained %d" % len(inbox)})
    for m in inbox:
        req("GET", "/messages/" + m["id"])            # mark read (like read_message)
        req("POST", "/messages", {"to": m["sender"], "body": "ack",
                                  "in_reply_to": m["id"]})
""")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def check(label, cond):
    print(("  ok " if cond else "  FAIL ") + label)
    if not cond:
        raise SystemExit(1)


async def poll(fn, want, timeout=15.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if want(await fn()):
            return True
        await asyncio.sleep(0.1)
    return False


async def main():
    port = free_port()
    os.environ["POSTBOX_HOST"] = "127.0.0.1"
    os.environ["POSTBOX_PORT"] = str(port)
    os.environ["POSTBOX_AGENT_COOLDOWN"] = "0"
    os.environ["POSTBOX_DATA_DIR"] = f"/tmp/postbox-fleet-e2e-{port}"
    os.environ.pop("POSTBOX_OBSERVER_TOKEN", None)   # open for the proof
    from postbox.api import create_app

    app = create_app()
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on")
    server = uvicorn.Server(cfg)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    base = f"http://127.0.0.1:{port}"
    print(f"server up on {base}")
    try:
        async with httpx.AsyncClient(base_url=base) as c:
            boss = (await c.post("/agents", json={"name": "boss"})).json()
            bh = {"Authorization": f"Bearer {boss['token']}"}

            # add the fleet agent DISABLED, so mail can pile up before any turn runs
            cmd = [sys.executable, "-c", FAKE_AGENT]
            r = await c.post("/fleet", json={"address": "alice", "command": cmd})
            check("POST /fleet registers alice", r.status_code == 201)
            await c.post("/fleet/alice/disable")

            N = 3
            for i in range(N):
                await c.post("/messages", headers=bh,
                             json={"to": "alice", "body": f"task {i}", "subject": "work"})
            fleet = (await c.get("/fleet")).json()
            alice = next(x for x in fleet if x["address"] == "alice")
            check("alice queued while disabled, no turn yet",
                  alice["state"] == "disabled" and alice["last_run"] is None)

            # enable → exactly one turn should spawn and drain all N
            await c.post("/fleet/alice/enable")

            async def boss_inbox():
                return (await c.get("/inbox", headers=bh)).json()

            got = await poll(boss_inbox, lambda ms: len(ms) >= N + 1)  # N acks + 1 marker
            inbox = await boss_inbox()
            check(f"boss received all replies ({len(inbox)} msgs)", got)

            senders = {m["sender"] for m in inbox}
            check("every reply is FROM alice (token identity injection works)",
                  senders == {"alice"})

            markers = [m for m in inbox if m["subject"] == "turn"]
            acks = [m for m in inbox if m["body"] == "ack"]
            check(f"exactly ONE turn drained all {N} messages (coalescing)",
                  len(markers) == 1 and markers[0]["body"] == f"drained {N}")
            check(f"got {N} acks, one per task", len(acks) == N)

            # a moment for the turn's exit to be recorded
            await poll(lambda: c.get("/fleet"),
                       lambda r: next(x for x in r.json() if x["address"] == "alice")["last_exit"] == 0)
            fleet = (await c.get("/fleet")).json()
            alice = next(x for x in fleet if x["address"] == "alice")
            check("alice turn exited 0, no failures, back to idle",
                  alice["last_exit"] == 0 and alice["fail_count"] == 0
                  and alice["state"] == "idle")

        print("\nALL FLEET E2E CHECKS PASSED")
    finally:
        server.should_exit = True
        await task


if __name__ == "__main__":
    asyncio.run(main())
