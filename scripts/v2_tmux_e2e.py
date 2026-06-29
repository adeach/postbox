"""Real-tmux end-to-end proof of v2: a Session's wakeup loop pokes a live tmux
pane when mail arrives. Not a unit test; run manually (needs tmux)."""
import asyncio
import os
import tempfile

import httpx
import uvicorn

from courier.api import create_app
from courier.mcp_server import Session

OK = "\033[92mPASS\033[0m"


async def main():
    app = create_app(tempfile.mkdtemp())
    cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="on")
    server = uvicorn.Server(cfg)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    sess_name = "courier_v2_e2e"
    outfile = os.path.join(tempfile.mkdtemp(), "pane.txt")
    await (await asyncio.create_subprocess_exec(
        "tmux", "new-session", "-d", "-s", sess_name, f"cat > {outfile}")).wait()
    await asyncio.sleep(0.3)
    proc = await asyncio.create_subprocess_exec(
        "tmux", "list-panes", "-t", sess_name, "-F", "#{pane_id}",
        stdout=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    pane = out.decode().split()[0]
    print(f"bob's tmux pane: {pane}")

    async with httpx.AsyncClient(base_url=base) as c:
        alice = (await c.post("/agents", json={"name": "alice"})).json()
        bob = Session(c, pane=pane, desired_name="bob")     # real TmuxWakeup
        await bob.start()
        await asyncio.sleep(0.3)
        await c.post("/messages", headers={"Authorization": f"Bearer {alice['token']}"},
                     json={"to": "bob", "body": "review PR #42?", "subject": "review"})
        await asyncio.sleep(0.6)
        await (await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", pane, "C-d")).wait()
        await asyncio.sleep(0.2)
        received = open(outfile).read()
        print(f"  [{OK if 'alice' in received else 'FAIL'}] bob's pane was poked: {received.strip()!r}")
        assert "alice" in received
        await bob.stop()

    await (await asyncio.create_subprocess_exec(
        "tmux", "kill-session", "-t", sess_name)).wait()
    server.should_exit = True
    await task
    print("\n\033[92mV2 REAL-TMUX E2E PASSED\033[0m — idle pane poked in real time on new mail.\n")


if __name__ == "__main__":
    asyncio.run(main())
