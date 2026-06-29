import asyncio
import pytest
from courier.events import EventBus, Event


@pytest.fixture(autouse=True)
async def _seed_agents(db):
    for aid in ("a1", "a2"):
        await db.execute(
            "INSERT INTO agents(id,name,address,token_hash,created_at) "
            "VALUES (?,?,?,?,?)",
            (aid, aid.upper(), aid, "h-" + aid, "2026-01-01T00:00:00Z"),
        )


async def test_append_returns_monotonic_ids(db):
    bus = EventBus(db)
    e1 = await bus.append("a1", "message.received", {"x": 1})
    e2 = await bus.append("a1", "message.received", {"x": 2})
    assert e2.id > e1.id


async def test_load_after(db):
    bus = EventBus(db)
    e1 = await bus.append("a1", "t", {})
    e2 = await bus.append("a1", "t", {})
    await bus.append("a2", "t", {})  # other agent — must not appear
    got = await bus.load_after("a1", after_id=e1.id)
    assert [e.id for e in got] == [e2.id]


async def test_live_publish_to_subscriber(db):
    bus = EventBus(db)
    q = bus.subscribe("a1")
    e = await bus.append("a1", "t", {"k": "v"})
    await bus.publish(e)
    received = await asyncio.wait_for(q.get(), timeout=1)
    assert received.id == e.id
    bus.unsubscribe("a1", q)


async def test_stream_replays_then_lives_without_dup(db):
    """Reconnect with last_event_id: must replay missed events exactly once,
    then deliver new live events, with no duplicates across the handoff."""
    bus = EventBus(db)
    missed = await bus.append("a1", "t", {"n": 1})   # happened while disconnected

    events = []
    async def consume():
        async for ev in bus.stream("a1", last_event_id=None):
            events.append(ev)
            if len(events) == 2:
                break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)                          # let stream subscribe+replay
    live = await bus.append("a1", "t", {"n": 2})
    await bus.publish(live)
    await asyncio.wait_for(task, timeout=2)

    ids = [e.id for e in events]
    assert ids == [missed.id, live.id]                 # ordered, no dup


async def test_stream_drops_already_replayed_live_event(db):
    """Directly exercise the dedup branch: an event that was replayed AND then
    also arrives live (id <= replayed_max) must be dropped, not re-yielded.
    Without the dedup guard this test yields the duplicate and fails."""
    bus = EventBus(db)
    replayed = await bus.append("a1", "t", {"n": 1})   # committed → will be replayed

    agen = bus.stream("a1", last_event_id=0)
    first = await agen.__anext__()                     # subscribe + replay → yields replayed
    assert first.id == replayed.id

    await bus.publish(replayed)                        # same event arrives live → must drop
    newer = await bus.append("a1", "t", {"n": 2})
    await bus.publish(newer)

    nxt = await agen.__anext__()                       # skips the dup, yields the newer one
    assert nxt.id == newer.id
    await agen.aclose()
