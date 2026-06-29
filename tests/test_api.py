import pytest
from httpx import ASGITransport, AsyncClient
from courier.api import create_app


@pytest.fixture
async def client(tmp_path):
    app = create_app(str(tmp_path / "data"))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            yield c


async def _register(client, name, address):
    r = await client.post("/agents", json={"name": name, "address": address})
    assert r.status_code == 201
    return r.json()


async def test_register_and_directory(client):
    a = await _register(client, "Claude", "claude")
    assert a["token"]
    r = await client.get("/agents")
    assert r.status_code == 200
    assert any(x["address"] == "claude" for x in r.json())


async def test_send_read_flow_with_auth(client):
    a = await _register(client, "A", "a")
    b = await _register(client, "B", "b")
    ah = {"Authorization": f"Bearer {a['token']}"}
    bh = {"Authorization": f"Bearer {b['token']}"}

    r = await client.post("/messages", headers=ah,
                          json={"to": "b", "body": "hello", "subject": "hi"})
    assert r.status_code == 201
    mid = r.json()["id"]

    r = await client.get("/inbox", headers=bh)
    assert [m["body"] for m in r.json()] == ["hello"]

    r = await client.get(f"/messages/{mid}", headers=bh)
    assert r.json()["read_at"] is not None


async def test_missing_token_rejected(client):
    await _register(client, "A", "a")
    r = await client.post("/messages", json={"to": "a", "body": "x"})
    assert r.status_code == 401


async def test_read_permissions(client):
    a = await _register(client, "A", "a")
    b = await _register(client, "B", "b")
    c = await _register(client, "C", "c")
    ah = {"Authorization": f"Bearer {a['token']}"}
    bh = {"Authorization": f"Bearer {b['token']}"}
    ch = {"Authorization": f"Bearer {c['token']}"}
    mid = (await client.post("/messages", headers=ah,
                             json={"to": "b", "body": "x"})).json()["id"]
    # recipient reads → 200 and marks read
    r = await client.get(f"/messages/{mid}", headers=bh)
    assert r.status_code == 200 and r.json()["read_at"] is not None
    # sender may view their own message → 200
    r = await client.get(f"/messages/{mid}", headers=ah)
    assert r.status_code == 200
    # unrelated third agent → 403
    r = await client.get(f"/messages/{mid}", headers=ch)
    assert r.status_code == 403
