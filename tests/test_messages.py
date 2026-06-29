import pytest
from courier.agents import AgentService
from courier.events import EventBus
from courier.messages import MessageService
from courier.models import RegisterAgent, SendMessage


@pytest.fixture
async def services(db):
    agents = AgentService(db)
    bus = EventBus(db)
    msgs = MessageService(db, agents, bus)
    a = await agents.register(RegisterAgent(name="A", address="a"))
    b = await agents.register(RegisterAgent(name="B", address="b"))
    return agents, bus, msgs, a, b


async def test_send_creates_inbox_entry_and_event(services):
    agents, bus, msgs, a, b = services
    m = await msgs.send(a.id, SendMessage(to="b", body="hello", subject="hi"))
    assert m.thread_id == m.id            # new thread
    inbox = await msgs.inbox(b.id)
    assert [x.body for x in inbox] == ["hello"]
    events = await bus.load_after(b.id, 0)
    assert any(e.type == "message.received" for e in events)


async def test_send_to_unknown_recipient_raises(services):
    agents, bus, msgs, a, b = services
    with pytest.raises(ValueError):
        await msgs.send(a.id, SendMessage(to="ghost", body="x"))


async def test_idempotent_send(services):
    agents, bus, msgs, a, b = services
    m1 = await msgs.send(a.id, SendMessage(to="b", body="x", idempotency_key="k1"))
    m2 = await msgs.send(a.id, SendMessage(to="b", body="x", idempotency_key="k1"))
    assert m1.id == m2.id
    assert len(await msgs.inbox(b.id)) == 1


async def test_read_marks_read_and_emits_receipt_to_sender(services):
    agents, bus, msgs, a, b = services
    m = await msgs.send(a.id, SendMessage(to="b", body="x"))
    read = await msgs.read(b.id, m.id)
    assert read.read_at is not None
    sender_events = await bus.load_after(a.id, 0)
    assert any(e.type == "message.read" for e in sender_events)


async def test_read_by_non_participant_forbidden(services):
    agents, bus, msgs, a, b = services
    c = await agents.register(RegisterAgent(name="C", address="c"))
    m = await msgs.send(a.id, SendMessage(to="b", body="x"))
    with pytest.raises(PermissionError):
        await msgs.read(c.id, m.id)        # c is neither sender nor recipient


async def test_sender_can_view_own_message_without_marking(services):
    agents, bus, msgs, a, b = services
    m = await msgs.send(a.id, SendMessage(to="b", body="x"))
    viewed = await msgs.read(a.id, m.id)   # sender views; no marking
    assert viewed.read_at is None
    assert len(await msgs.inbox(b.id, unread=True)) == 1  # still unread for b


async def test_reply_inherits_thread(services):
    agents, bus, msgs, a, b = services
    m = await msgs.send(a.id, SendMessage(to="b", body="q", subject="Q"))
    r = await msgs.send(b.id, SendMessage(to="a", body="re", in_reply_to=m.id))
    assert r.thread_id == m.thread_id
    assert r.subject == "Q"                # inherited
    thread = await msgs.thread(a.id, m.thread_id)
    assert [x.body for x in thread] == ["q", "re"]


async def test_unread_filter(services):
    agents, bus, msgs, a, b = services
    m = await msgs.send(a.id, SendMessage(to="b", body="x"))
    assert len(await msgs.inbox(b.id, unread=True)) == 1
    await msgs.read(b.id, m.id)
    assert len(await msgs.inbox(b.id, unread=True)) == 0
