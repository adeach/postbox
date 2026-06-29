import pytest
from httpx import ASGITransport, AsyncClient
from courier.api import create_app
from courier.mcp_server import MailTools


@pytest.fixture
async def tools(tmp_path):
    app = create_app(str(tmp_path / "data"))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            a = (await c.post("/agents", json={"name": "A", "address": "a"})).json()
            b = (await c.post("/agents", json={"name": "B", "address": "b"})).json()
            yield MailTools(c, a["token"]), MailTools(c, b["token"])


async def test_list_agents_tool(tools):
    a_mail, _ = tools
    agents = await a_mail.list_agents()
    assert {x["address"] for x in agents} == {"a", "b"}


async def test_send_then_recipient_sees_in_inbox(tools):
    a_mail, b_mail = tools
    await a_mail.send_message(to="b", body="hi", subject="s")
    inbox = await b_mail.check_inbox(unread=True)
    assert [m["body"] for m in inbox] == ["hi"]


async def test_reply_threads_and_routes_back(tools):
    a_mail, b_mail = tools
    m = await a_mail.send_message(to="b", body="q", subject="Q")
    r = await b_mail.reply(message_id=m["id"], body="re")  # B reads original, replies to A
    assert r["thread_id"] == m["thread_id"]
    a_inbox = await a_mail.check_inbox(unread=True)
    assert [x["body"] for x in a_inbox] == ["re"]


import asyncio
import uvicorn
from courier.mcp_server import Session


async def test_session_autoregisters_with_pane_and_wakes(tmp_path):
    """Session registers itself (capturing the pane), and its wakeup loop pokes
    the pane when mail arrives — verified with an injected fake tmux runner.

    IMPORTANT: this drives a live SSE stream, so it MUST run against a real
    uvicorn socket — httpx ASGITransport buffers responses and cannot stream
    SSE (it would hang). Same reason test_sse.py uses a real server.
    """
    app = create_app(str(tmp_path / "data"))
    cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="on")
    server = uvicorn.Server(cfg)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    try:
        async with AsyncClient(base_url=base) as c:
            alice = (await c.post("/agents", json={"name": "alice"})).json()
            ah = {"Authorization": f"Bearer {alice['token']}"}

            pokes = []
            async def fake_run(cmd): pokes.append(cmd)

            # bob's session auto-registers with pane %42 and a tmux wakeup using fake_run
            bob = Session(client=c, pane="%42", desired_name="bob", runner=fake_run)
            await bob.start()
            try:
                assert bob.token                        # got a token
                assert await bob.tools.list_agents()    # directory reachable
                await asyncio.sleep(0.2)                # let the SSE loop subscribe
                await c.post("/messages", headers=ah, json={"to": "bob", "body": "ping"})
                await asyncio.sleep(0.5)                # let the wakeup fire
                assert any("%42" in cmd for cmd in pokes)         # poked bob's pane
                assert any("alice" in str(cmd) for cmd in pokes)  # names the sender
            finally:
                await bob.stop()
            online = (await c.get("/agents")).json()    # bob offline after stop
            assert all(a["address"] != "bob" for a in online)
    finally:
        server.should_exit = True
        await task
