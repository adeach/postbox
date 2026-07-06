from types import SimpleNamespace

import pytest

from postbox.agents import AgentService
from postbox.events import EventBus
from postbox.federation import FederationService
from postbox.messages import MessageService
from postbox.models import RegisterAgent, SendMessage
from postbox.peers import PeerService


@pytest.fixture
async def services(db):
    agents = AgentService(db)
    bus = EventBus(db)
    messages = MessageService(db, agents, bus)
    peers = PeerService(db)
    calls = []

    async def relay(url, token, payload):
        calls.append((url, token, payload))

    settings = SimpleNamespace(instance="postbox1")
    federation = FederationService(
        db, agents, messages, peers, bus, settings, relay=relay)
    messages.federation = federation
    alice = await agents.register(RegisterAgent(name="alice"))
    bob = await agents.register(RegisterAgent(name="bob"))
    return agents, bus, messages, peers, federation, calls, alice, bob


async def test_is_remote_classifies_local_self_and_remote(services):
    *_, federation, _calls, _alice, _bob = services
    assert federation.is_remote("bob") is None
    assert federation.is_remote("bob@postbox1") is None
    assert federation.is_remote("bob@postbox2") == ("bob", "postbox2")


async def test_send_remote_unknown_peer_raises(services):
    _agents, _bus, messages, _peers, _fed, _calls, alice, _bob = services
    with pytest.raises(ValueError, match="unknown peer: postbox2"):
        await messages.send(alice.id, SendMessage(to="bob@postbox2", body="hi"))


async def test_send_remote_requires_instance(services):
    _agents, _bus, messages, peers, federation, _calls, alice, _bob = services
    await peers.upsert("postbox2", "http://postbox2", "tok")
    federation.settings = SimpleNamespace(instance=None)

    with pytest.raises(ValueError, match="federation not configured"):
        await messages.send(alice.id, SendMessage(to="bob@postbox2", body="hi"))


async def test_send_remote_stores_local_copy_and_relays(services):
    agents, _bus, messages, peers, _fed, calls, alice, _bob = services
    await peers.upsert("postbox2", "http://postbox2", "tok")

    msg = await messages.send(
        alice.id, SendMessage(to="bob@postbox2", body="hi", subject="Hello"))

    stub = await agents.get_by_address("bob@postbox2")
    assert stub is not None
    inbox = await messages.inbox(stub.id)
    assert [m.id for m in inbox] == [msg.id]
    assert (await messages.thread(alice.id, msg.thread_id))[0].body == "hi"
    assert len(calls) == 1
    url, token, payload = calls[0]
    assert (url, token) == ("http://postbox2", "tok")
    assert payload["from"] == "alice@postbox1"
    assert payload["to"] == "bob"
    assert payload["fed_thread_id"] == msg.thread_id
    assert payload["origin_msg_id"] == msg.id


async def test_send_remote_soft_succeeds_when_relay_raises(db):
    agents = AgentService(db)
    bus = EventBus(db)
    messages = MessageService(db, agents, bus)
    peers = PeerService(db)
    alice = await agents.register(RegisterAgent(name="alice"))
    await peers.upsert("postbox2", "http://postbox2", "tok")

    async def relay(_url, _token, _payload):
        raise RuntimeError("down")

    federation = FederationService(
        db, agents, messages, peers, bus,
        SimpleNamespace(instance="postbox1"), relay=relay)
    messages.federation = federation

    msg = await messages.send(alice.id, SendMessage(to="bob@postbox2", body="hi"))

    stub = await agents.get_by_address("bob@postbox2")
    assert stub is not None
    assert [m.id for m in await messages.inbox(stub.id)] == [msg.id]


async def test_inbound_valid_creates_stub_delivers_and_emits_event(services):
    agents, bus, messages, peers, federation, _calls, _alice, bob = services
    await peers.upsert("postbox2", "http://postbox2", "tok")

    res = await federation.inbound("tok", {
        "from": "alice@postbox2",
        "to": "bob",
        "body": "hello",
        "subject": "S",
        "content_type": "text/plain",
        "fed_thread_id": "thread-1",
        "origin_msg_id": "origin-1",
    })

    stub = await agents.get_by_address("alice@postbox2")
    assert stub is not None
    inbox = await messages.inbox(bob.id)
    assert len(inbox) == 1
    assert inbox[0].id == res["message_id"]
    assert inbox[0].thread_id == "thread-1"
    assert inbox[0].sender == "alice@postbox2"
    events = await bus.load_after(bob.id, 0)
    assert [e.type for e in events] == ["message.received"]


async def test_inbound_is_idempotent_on_origin_msg_id(services):
    _agents, _bus, messages, peers, federation, _calls, _alice, bob = services
    await peers.upsert("postbox2", "http://postbox2", "tok")
    body = {
        "from": "alice@postbox2",
        "to": "bob",
        "body": "hello",
        "fed_thread_id": "thread-1",
        "origin_msg_id": "origin-1",
    }

    first = await federation.inbound("tok", body)
    second = await federation.inbound("tok", body)

    assert second == first
    assert len(await messages.inbox(bob.id)) == 1


async def test_inbound_rejects_bad_token(services):
    _agents, _bus, _messages, peers, federation, _calls, _alice, _bob = services
    await peers.upsert("postbox2", "http://postbox2", "tok")

    with pytest.raises(PermissionError, match="unknown peer token"):
        await federation.inbound("bad", {})


async def test_inbound_rejects_from_domain_mismatch(services):
    _agents, _bus, _messages, peers, federation, _calls, _alice, _bob = services
    await peers.upsert("postbox2", "http://postbox2", "tok")

    with pytest.raises(PermissionError, match="from-domain"):
        await federation.inbound("tok", {
            "from": "alice@postbox3",
            "to": "bob",
            "body": "hello",
            "fed_thread_id": "thread-1",
            "origin_msg_id": "origin-1",
        })


async def test_inbound_unknown_local_recipient(services):
    _agents, _bus, _messages, peers, federation, _calls, _alice, _bob = services
    await peers.upsert("postbox2", "http://postbox2", "tok")

    with pytest.raises(LookupError, match="unknown local recipient: ghost"):
        await federation.inbound("tok", {
            "from": "alice@postbox2",
            "to": "ghost",
            "body": "hello",
            "fed_thread_id": "thread-1",
            "origin_msg_id": "origin-1",
        })
