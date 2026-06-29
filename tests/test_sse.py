import asyncio
import json
import pytest
import uvicorn
from httpx import AsyncClient
from httpx_sse import aconnect_sse
from postbox.api import create_app


@pytest.fixture
async def app_client(tmp_path):
    # NOTE: deviation from the plan's verbatim fixture. The plan used
    # httpx ASGITransport, but that transport buffers the entire response
    # (it awaits the ASGI app to completion before returning), so it can
    # never stream an unbounded SSE endpoint and the tests hang. Run the
    # app under a real in-process uvicorn server so the socket streams.
    # The two test bodies below are unchanged.
    app = create_app(str(tmp_path / "data"))
    config = uvicorn.Config(app, host="127.0.0.1", port=0,
                            log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
            yield app, c
    finally:
        server.should_exit = True
        await task


async def _reg(c, name, addr):
    r = await c.post("/agents", json={"name": name, "address": addr})
    return r.json()


async def test_live_event_delivered_over_sse(app_client):
    app, c = app_client
    a = await _reg(c, "A", "a")
    b = await _reg(c, "B", "b")
    bh = {"Authorization": f"Bearer {b['token']}"}

    received = []

    async def listen():
        async with aconnect_sse(c, "GET", "/events", headers=bh) as es:
            async for sse in es.aiter_sse():
                received.append(json.loads(sse.data))
                break

    task = asyncio.create_task(listen())
    await asyncio.sleep(0.1)  # ensure subscribed
    await c.post("/messages", headers={"Authorization": f"Bearer {a['token']}"},
                 json={"to": "b", "body": "ping", "subject": "s"})
    await asyncio.wait_for(task, timeout=3)
    assert received[0]["from"] == "a"


async def test_reconnect_replays_missed(app_client):
    app, c = app_client
    a = await _reg(c, "A", "a")
    b = await _reg(c, "B", "b")
    ah = {"Authorization": f"Bearer {a['token']}"}
    bh = {"Authorization": f"Bearer {b['token']}"}

    # event happens while B is NOT connected
    await c.post("/messages", headers=ah, json={"to": "b", "body": "missed"})

    received = []

    async def listen():
        # reconnect from the beginning (Last-Event-ID: 0)
        async with aconnect_sse(c, "GET", "/events",
                                headers={**bh, "Last-Event-ID": "0"}) as es:
            async for sse in es.aiter_sse():
                received.append(int(sse.id))
                break

    task = asyncio.create_task(listen())
    await asyncio.wait_for(task, timeout=3)
    assert len(received) == 1  # the missed event replayed exactly once
