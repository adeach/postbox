import pytest
from httpx import ASGITransport, AsyncClient

from postbox.api import create_app


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.delenv("POSTBOX_INSTANCE", raising=False)
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.yaml").write_text("instance: postbox2\n")
    app = create_app(str(data))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def _seed(client):
    bob = (await client.post("/agents", json={"name": "bob"})).json()
    await client.post("/peers", json={
        "name": "postbox1",
        "url": "http://postbox1",
        "token": "tok",
    })
    return bob


def _payload(**overrides):
    body = {
        "from": "alice@postbox1",
        "to": "bob",
        "body": "hello",
        "subject": "S",
        "content_type": "text/plain",
        "fed_thread_id": "thread-1",
        "origin_msg_id": "origin-1",
        "created_at": "2026-07-06T00:00:00Z",
    }
    body.update(overrides)
    return body


async def test_federation_inbound_happy_path(client):
    bob = await _seed(client)

    r = await client.post(
        "/federation/inbound",
        headers={"X-Postbox-Peer-Token": "tok"},
        json=_payload(),
    )

    assert r.status_code == 201
    assert r.json()["thread_id"] == "thread-1"
    inbox = (await client.get(
        "/inbox", headers={"Authorization": "Bearer " + bob["token"]})).json()
    assert len(inbox) == 1
    assert inbox[0]["thread_id"] == "thread-1"
    assert inbox[0]["sender"] == "alice@postbox1"


async def test_federation_inbound_bad_token_401(client):
    await _seed(client)

    r = await client.post(
        "/federation/inbound",
        headers={"X-Postbox-Peer-Token": "bad"},
        json=_payload(),
    )

    assert r.status_code == 401


async def test_federation_inbound_domain_mismatch_403(client):
    await _seed(client)

    r = await client.post(
        "/federation/inbound",
        headers={"X-Postbox-Peer-Token": "tok"},
        json=_payload(**{"from": "alice@postbox3"}),
    )

    assert r.status_code == 403


async def test_federation_inbound_unknown_recipient_404(client):
    await _seed(client)

    r = await client.post(
        "/federation/inbound",
        headers={"X-Postbox-Peer-Token": "tok"},
        json=_payload(to="ghost"),
    )

    assert r.status_code == 404


async def test_federation_inbound_dedupes_repeat(client):
    bob = await _seed(client)
    headers = {"X-Postbox-Peer-Token": "tok"}
    body = _payload()

    first = await client.post("/federation/inbound", headers=headers, json=body)
    second = await client.post("/federation/inbound", headers=headers, json=body)

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json()
    inbox = (await client.get(
        "/inbox", headers={"Authorization": "Bearer " + bob["token"]})).json()
    assert len(inbox) == 1
