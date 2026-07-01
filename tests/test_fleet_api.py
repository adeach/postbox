import pytest
from httpx import ASGITransport, AsyncClient

from postbox.api import create_app


@pytest.fixture
async def client(tmp_path):
    app = create_app(str(tmp_path / "data"))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def test_fleet_crud_over_http(client):
    r = await client.post("/fleet", json={"address": "alice", "command": ["true"]})
    assert r.status_code == 201
    alice = next(a for a in (await client.get("/fleet")).json() if a["address"] == "alice")
    assert alice["state"] == "idle" and alice["command"] == ["true"] and alice["enabled"]

    assert (await client.post("/fleet/alice/disable")).status_code == 200
    alice = next(a for a in (await client.get("/fleet")).json() if a["address"] == "alice")
    assert alice["state"] == "disabled" and alice["enabled"] is False

    assert (await client.post("/fleet/alice/enable")).status_code == 200
    assert (await client.post("/fleet/ghost/run")).status_code == 404      # unknown agent

    assert (await client.delete("/fleet/alice")).status_code == 204
    assert all(a["address"] != "alice" for a in (await client.get("/fleet")).json())


async def test_fleet_upsert_conflict_on_nonfleet_identity(client):
    await client.post("/agents", json={"name": "human1"})     # a plain identity
    r = await client.post("/fleet", json={"address": "human1"})
    assert r.status_code == 409                                # can't manage — token unknown


async def test_observer_token_guards_observer_and_fleet(tmp_path, monkeypatch):
    monkeypatch.setenv("POSTBOX_OBSERVER_TOKEN", "sekret")
    app = create_app(str(tmp_path / "data"))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.get("/fleet")).status_code == 401            # no token
            assert (await c.get("/observer/agents")).status_code == 401
            h = {"X-Observer-Token": "sekret"}
            assert (await c.get("/fleet", headers=h)).status_code == 200
            assert (await c.get("/observer/agents", headers=h)).status_code == 200
            assert (await c.get("/observer/agents?token=sekret")).status_code == 200  # EventSource path
            assert (await c.get("/fleet", headers={"X-Observer-Token": "nope"})).status_code == 401
            assert (await c.get("/agents")).status_code == 200          # bearer routes unaffected
