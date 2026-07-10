import pytest
from httpx import ASGITransport, AsyncClient
from postbox.api import create_app
from postbox.mcp_server import MailTools


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
import contextlib
import uvicorn
from postbox.mcp_server import Session


async def test_safety_loop_repokes_persistently_unread(monkeypatch):
    """Watchdog: mail that stays unread across a full poll interval gets re-poked
    (recovers a dropped SSE wake); fresh mail seen only once is NOT re-poked."""
    monkeypatch.setenv("POSTBOX_SAFETY_POLL", "0.05")

    class FakeResp:
        status_code = 200
        def json(self): return [{"id": "m1", "sender": "x", "subject": "s"}]

    class FakeClient:
        async def get(self, *a, **k): return FakeResp()

    s = Session(client=FakeClient(), pane="%1", desired_name="w")
    s.token = "tok"
    poked = []
    async def fake_poke(ev): poked.append(ev)
    s._poke = fake_poke
    t = asyncio.create_task(s._safety_loop())
    await asyncio.sleep(0.25)                       # several poll intervals
    t.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await t
    assert poked and poked[0]["message_id"] == "m1"    # stuck unread → re-poked


async def test_safety_loop_ignores_transient_unread(monkeypatch):
    """A message seen unread on only ONE poll (about to be handled by the SSE wake) is
    not re-poked — avoids double-poking normal in-flight mail."""
    monkeypatch.setenv("POSTBOX_SAFETY_POLL", "0.05")
    state = {"n": 0}

    class FakeResp:
        status_code = 200
        def __init__(self, msgs): self._m = msgs
        def json(self): return self._m

    class FakeClient:
        async def get(self, *a, **k):
            state["n"] += 1
            # unread on the first poll only, then inbox is clear
            return FakeResp([{"id": "m9", "sender": "x", "subject": "s"}] if state["n"] == 1 else [])

    s = Session(client=FakeClient(), pane="%1", desired_name="w")
    s.token = "tok"
    poked = []
    async def fake_poke(ev): poked.append(ev)
    s._poke = fake_poke
    t = asyncio.create_task(s._safety_loop())
    await asyncio.sleep(0.25)
    t.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await t
    assert poked == []                                 # never persisted → no re-poke


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


async def test_session_key_persists_on_stop_and_reattaches(tmp_path):
    """A session_key-bearing identity PERSISTS on stop (stays listed, resumable) and a
    second session with the same key reattaches to the SAME identity — the resume path.
    Contrast with the test above: a keyless session is deregistered (hidden) on stop.
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
            s1 = Session(client=c, pane=None, desired_name="carol", session_key="sess-carol")
            await s1.start()
            rows = (await c.get("/observer/agents")).json()
            carol = [a for a in rows if a["address"] == "carol"][0]
            assert carol["session_key"] == "sess-carol"
            agent_id = carol["id"]
            await s1.stop()
            # PERSISTED: still listed after stop (not deregistered/hidden)
            listed = (await c.get("/agents")).json()
            assert any(a["address"] == "carol" for a in listed)
            # RESUME: same key → same identity (reattach), fresh token
            s2 = Session(client=c, pane=None, desired_name="carol", session_key="sess-carol")
            await s2.start()
            try:
                again = [a for a in (await c.get("/observer/agents")).json()
                         if a["address"] == "carol"][0]
                assert again["id"] == agent_id       # same row, not a duplicate
            finally:
                await s2.stop()
    finally:
        server.should_exit = True
        await task
