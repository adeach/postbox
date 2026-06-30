import asyncio
import pytest
import uvicorn
from httpx import ASGITransport, AsyncClient
from httpx_sse import aconnect_sse
from postbox.api import create_app


@pytest.fixture
async def client(tmp_path):
    app = create_app(str(tmp_path / "data"))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def _reg(c, name):
    return (await c.post("/agents", json={"name": name})).json()


async def test_observer_threads_and_detail(client):
    a = await _reg(client, "alice"); b = await _reg(client, "bob")
    ah = {"Authorization": f"Bearer {a['token']}"}
    r = await client.post("/messages", headers=ah,
                          json={"to": b["address"], "body": "hi", "subject": "s"})
    tid = r.json()["thread_id"]
    threads = (await client.get("/observer/threads")).json()
    assert any(t["thread_id"] == tid for t in threads)
    detail = (await client.get(f"/observer/threads/{tid}")).json()
    assert detail["messages"][0]["from"] == a["address"]


async def test_observer_threads_filtered(client):
    a = await _reg(client, "alice"); b = await _reg(client, "bob"); c = await _reg(client, "carol")
    ah = {"Authorization": f"Bearer {a['token']}"}
    await client.post("/messages", headers=ah, json={"to": b["address"], "body": "x"})
    carol_threads = (await client.get("/observer/threads",
                                      params={"address": c["address"]})).json()
    assert carol_threads == []   # carol isn't in any thread


async def test_observer_agents(client):
    await _reg(client, "alice")
    rows = (await client.get("/observer/agents")).json()
    assert any(x["address"] == "alice" for x in rows)


async def test_create_identity_and_send_as(client):
    a = await _reg(client, "alice")
    me = (await client.post("/observer/identity", json={"name": "adam"})).json()
    assert me["address"] == "adam"
    s = await client.post("/observer/send",
                          json={"from": "adam", "to": "alice", "body": "hello"})
    assert s.status_code == 201
    # alice received it
    threads = (await client.get("/observer/threads", params={"address": "alice"})).json()
    assert any("adam" in t["members"] for t in threads)


async def test_send_as_unknown_sender_400(client):
    await _reg(client, "alice")
    r = await client.post("/observer/send",
                          json={"from": "ghost", "to": "alice", "body": "x"})
    assert r.status_code == 400


async def test_observer_read_marks_human_and_rejects_agent(client):
    a = await _reg(client, "alice")
    ah = {"Authorization": f"Bearer {a['token']}"}
    adam = (await client.post("/observer/identity", json={"name": "adam"})).json()
    # alice messages the human adam
    r = await client.post("/messages", headers=ah,
                          json={"to": "adam", "body": "for you", "subject": "s"})
    tid = r.json()["thread_id"]
    # before: adam's message is unread
    d0 = (await client.get(f"/observer/threads/{tid}")).json()
    assert d0["messages"][0]["read_by"] == []
    # human opens it -> marked read
    rd = await client.post("/observer/read", json={"as": "adam", "thread_id": tid})
    assert rd.status_code == 200 and rd.json()["marked"] == 1
    d1 = (await client.get(f"/observer/threads/{tid}")).json()
    assert d1["messages"][0]["read_by"] == ["adam"]
    # a real agent may NOT mark read via the observer (would corrupt its state)
    bad = await client.post("/observer/read", json={"as": "alice", "thread_id": tid})
    assert bad.status_code == 403


async def test_ui_served(client):
    r = await client.get("/ui/")
    assert r.status_code == 200 and "Postbox" in r.text


async def test_observer_firehose_sse(tmp_path):
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
            a = await _reg(c, "alice"); b = await _reg(c, "bob")
            received = []
            async def listen():
                async with aconnect_sse(c, "GET", "/observer/events") as es:
                    async for sse in es.aiter_sse():
                        received.append(sse.event); break
            t = asyncio.create_task(listen())
            await asyncio.sleep(0.15)
            await c.post("/messages", headers={"Authorization": f"Bearer {a['token']}"},
                         json={"to": b["address"], "body": "hi"})
            await asyncio.wait_for(t, timeout=3)
            assert received[0] == "message.received"
    finally:
        server.should_exit = True
        await task
