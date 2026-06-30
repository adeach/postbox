import pytest
from postbox.agents import AgentService
from postbox.events import EventBus
from postbox.messages import MessageService
from postbox.observer import ObserverService
from postbox.models import RegisterAgent, SendMessage


@pytest.fixture
async def world(db):
    agents = AgentService(db)
    bus = EventBus(db)
    msgs = MessageService(db, agents, bus)
    obs = ObserverService(db, agents, msgs)
    a = await agents.register(RegisterAgent(name="alice"))
    b = await agents.register(RegisterAgent(name="bob"))
    c = await agents.register(RegisterAgent(name="carol"))
    # alice<->bob thread, and bob<->carol thread
    m1 = await msgs.send(a.id, SendMessage(to="bob", body="hi bob", subject="t1"))
    await msgs.send(b.id, SendMessage(to="alice", body="hi alice", in_reply_to=m1.id))
    await msgs.send(b.id, SendMessage(to="carol", body="hi carol", subject="t2"))
    return agents, msgs, obs, a, b, c, m1


async def test_list_all_threads(world):
    agents, msgs, obs, a, b, c, m1 = world
    threads = await obs.list_threads()           # all activity
    subjects = {t.subject for t in threads}
    assert {"t1", "t2"} <= subjects
    t1 = next(t for t in threads if t.subject == "t1")
    assert set(t1.members) == {"alice", "bob"}
    assert t1.message_count == 2
    assert t1.last["from"] == "bob" and t1.last["text"] == "hi alice"


async def test_list_threads_for_identity(world):
    agents, msgs, obs, a, b, c, m1 = world
    # carol only participates in t2
    ct = await obs.list_threads(address="carol")
    assert {t.subject for t in ct} == {"t2"}
    # bob is in both
    bt = await obs.list_threads(address="bob")
    assert {t.subject for t in bt} == {"t1", "t2"}


async def test_unread_counts(world):
    agents, msgs, obs, a, b, c, m1 = world
    t1 = next(t for t in await obs.list_threads() if t.subject == "t1")
    # alice received bob's reply (unread), bob received alice's first (unread)
    assert t1.unread.get("alice", 0) == 1
    assert t1.unread.get("bob", 0) == 1


async def test_thread_detail(world):
    agents, msgs, obs, a, b, c, m1 = world
    d = await obs.thread(m1.thread_id)
    assert [m.body for m in d.messages] == ["hi bob", "hi alice"]
    assert d.messages[0].from_ == "alice" and d.messages[0].to == ["bob"]


async def test_create_identity_persists(world):
    agents, msgs, obs, a, b, c, m1 = world
    res = await obs.create_identity("adam")
    assert res.address == "adam"
    assert any(x.address == "adam" for x in await obs.agents_all())


async def test_send_as_delivers(world):
    agents, msgs, obs, a, b, c, m1 = world
    sent = await obs.send_as("carol", "alice", "ping from carol", subject="hey")
    inbox = await msgs.inbox(a.id, unread=True)
    assert any(m.body == "ping from carol" and m.sender == "carol" for m in inbox)


async def test_send_as_unknown_sender(world):
    agents, msgs, obs, a, b, c, m1 = world
    with pytest.raises(ValueError):
        await obs.send_as("ghost", "alice", "x")
