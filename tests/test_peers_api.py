import pytest
from httpx import ASGITransport, AsyncClient

from postbox.api import create_app


@pytest.fixture
async def client(tmp_path):
    app = create_app(str(tmp_path / "data"))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def test_peers_crud_over_http_and_redacts_token(client):
    r = await client.post("/peers", json={
        "name": "east",
        "url": "http://east.example",
        "token": "shared-secret",
    })
    assert r.status_code == 201
    assert r.json() == {"name": "east", "url": "http://east.example"}

    r = await client.get("/peers")
    assert r.status_code == 200
    assert r.json() == [{"name": "east", "url": "http://east.example"}]
    assert "token" not in r.json()[0]

    r = await client.post("/peers", json={
        "name": "east",
        "url": "http://east2.example",
        "token": "new-secret",
    })
    assert r.status_code == 201
    assert r.json() == {"name": "east", "url": "http://east2.example"}

    assert (await client.delete("/peers/east")).status_code == 204
    assert (await client.get("/peers")).json() == []


async def test_observer_token_guards_peers(tmp_path, monkeypatch):
    monkeypatch.setenv("POSTBOX_OBSERVER_TOKEN", "sekret")
    app = create_app(str(tmp_path / "data"))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.get("/peers")).status_code == 401
            assert (await c.post("/peers", json={
                "name": "east",
                "url": "http://east.example",
                "token": "shared-secret",
            })).status_code == 401
            assert (await c.delete("/peers/east")).status_code == 401

            h = {"X-Observer-Token": "sekret"}
            assert (await c.get("/peers", headers=h)).status_code == 200
            assert (await c.post("/peers", headers=h, json={
                "name": "east",
                "url": "http://east.example",
                "token": "shared-secret",
            })).status_code == 201
            assert (await c.delete("/peers/east", headers=h)).status_code == 204
