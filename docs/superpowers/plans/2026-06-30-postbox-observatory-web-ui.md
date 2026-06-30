# Postbox Observatory — Web UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add a local, Slack-themed web app — the Observatory — that lets a human open as any identity, read every conversation in the system (or one identity's inbox), reply as the open identity, and get live updates.

**Architecture:** A new `ObserverService` provides global (identity-agnostic) reads + send-as over the existing SQLite/MessageService/EventBus. The FastAPI app gains `/observer/*` JSON endpoints, an `/observer/events` SSE firehose (all events), and serves a static vanilla HTML/CSS/JS client at `/ui/`. No schema change, no frontend framework, no build step.

**Tech Stack:** Python/FastAPI/aiosqlite/sse-starlette (backend, unchanged); vanilla HTML/CSS/JS served by `fastapi.staticfiles.StaticFiles`.

**Verified facts:** the existing `messages.thread_id` groups conversations; `recipients(read_at)` gives unread; `MessageService.send(sender_id, SendMessage)` already emits events + wakes the recipient. The Observatory reuses all of it.

---

## File Structure
```
postbox/events.py        MODIFY  firehose: publish→firehose, subscribe_all/unsubscribe_all, load_all_after, stream_all
postbox/models.py        MODIFY  observer models: AgentFull, ThreadSummary, ThreadDetail, MessageView, SendAs, CreateIdentity
postbox/observer.py      CREATE  ObserverService: agents, list_threads, thread, create_identity, send_as
postbox/api.py           MODIFY  /observer/* routes + /observer/events SSE + StaticFiles /ui
postbox/web/index.html   CREATE  Slack Observatory markup
postbox/web/styles.css   CREATE  aubergine theme (from mockups/8)
postbox/web/app.js       CREATE  fetch + SSE wiring
tests/test_events.py     MODIFY  firehose test
tests/test_observer.py   CREATE  ObserverService unit tests
tests/test_observer_api.py CREATE observer API + firehose SSE + static serving tests
scripts/observer_e2e.py  CREATE  live proof harness
README.md                MODIFY  how to open the web UI
CLAUDE.md                MODIFY  index
```

---

## Task 1: EventBus firehose (global event stream)

**Files:** Modify `postbox/events.py`; Test `tests/test_events.py`

- [ ] **Step 1: Write failing tests** (append to `tests/test_events.py`)

```python
async def test_firehose_receives_all_agents(db):
    bus = EventBus(db)
    q = bus.subscribe_all()
    e1 = await bus.append("a1", "message.received", {"n": 1})
    await bus.publish(e1)
    e2 = await bus.append("a2", "message.received", {"n": 2})
    await bus.publish(e2)
    got = [await asyncio.wait_for(q.get(), 1), await asyncio.wait_for(q.get(), 1)]
    assert [e.agent_id for e in got] == ["a1", "a2"]   # firehose = ALL agents
    bus.unsubscribe_all(q)


async def test_load_all_after(db):
    bus = EventBus(db)
    e1 = await bus.append("a1", "t", {})
    e2 = await bus.append("a2", "t", {})
    got = await bus.load_all_after(e1.id)
    assert [e.id for e in got] == [e2.id]


async def test_stream_all_replays_then_lives(db):
    bus = EventBus(db)
    missed = await bus.append("a1", "t", {"n": 1})  # before connect
    events = []
    async def consume():
        async for ev in bus.stream_all(last_event_id=None):
            events.append(ev)
            if len(events) == 2:
                break
    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    live = await bus.append("a2", "t", {"n": 2})
    await bus.publish(live)
    await asyncio.wait_for(task, timeout=2)
    assert [e.id for e in events] == [missed.id, live.id]
```

(Requires the `_seed_agents` autouse fixture already in `tests/test_events.py` — `a1`/`a2` exist; add `"a2"` is already seeded. If `load_all_after`'s rows reference agents, they're seeded.)

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/pytest tests/test_events.py -v`
Expected: FAIL — `subscribe_all`/`load_all_after`/`stream_all` undefined.

- [ ] **Step 3: Implement in `postbox/events.py`**

Add a firehose set in `__init__` (alongside `self._subs`):
```python
        self._firehose: set[asyncio.Queue] = set()
```

In `publish`, after the per-agent loop, also fan out to the firehose:
```python
    async def publish(self, event: Event) -> None:
        for q in list(self._subs.get(event.agent_id, ())):
            await q.put(event)
        for q in list(self._firehose):
            await q.put(event)
```

Add these methods:
```python
    def subscribe_all(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._firehose.add(q)
        return q

    def unsubscribe_all(self, q: asyncio.Queue) -> None:
        self._firehose.discard(q)

    async def load_all_after(self, after_id: int) -> list[Event]:
        rows = await self.db.fetchall(
            "SELECT id,agent_id,type,payload,created_at FROM events "
            "WHERE id>? ORDER BY id",
            (after_id,),
        )
        return [Event(r[0], r[1], r[2], json.loads(r[3]), r[4]) for r in rows]

    async def stream_all(self, last_event_id: int | None):
        after = last_event_id or 0
        q = self.subscribe_all()                       # live first
        try:
            replayed_max = after
            for ev in await self.load_all_after(after): # replay backlog
                yield ev
                replayed_max = ev.id
            while True:                                 # flush live, dedup
                ev = await q.get()
                if ev.id <= replayed_max:
                    continue
                yield ev
                replayed_max = ev.id
        finally:
            self.unsubscribe_all(q)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_events.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add postbox/events.py tests/test_events.py
git commit -m "observatory: add EventBus firehose (subscribe_all + stream_all)"
```

---

## Task 2: Observer models

**Files:** Modify `postbox/models.py`; Test `tests/test_models.py`

- [ ] **Step 1: Write failing tests** (append to `tests/test_models.py`)

```python
def test_observer_models():
    from postbox.models import ThreadSummary, SendAs, CreateIdentity, MessageView
    s = ThreadSummary(thread_id="t1", subject="hi", members=["a", "b"],
                      last={"from": "a", "text": "yo", "at": "t"},
                      message_count=2, unread={"b": 1})
    assert s.members == ["a", "b"] and s.unread["b"] == 1
    assert SendAs(**{"from": "a", "to": "b", "body": "x"}).from_ == "a"
    assert CreateIdentity(name="adam").name == "adam"
    m = MessageView(id="m1", from_="a", to=["b"], subject=None, body="x",
                    content_type="text/plain", created_at="t", read_by=[])
    assert m.from_ == "a"
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/pytest tests/test_models.py -v`
Expected: FAIL — models undefined.

- [ ] **Step 3: Implement in `postbox/models.py`** (append)

```python
class AgentFull(BaseModel):
    id: str
    name: str
    address: str
    profile: dict | None = None
    status: str = "online"


class ThreadSummary(BaseModel):
    thread_id: str
    subject: str | None
    members: list[str]
    last: dict           # {"from": addr, "text": str, "at": iso}
    message_count: int
    unread: dict[str, int]   # address -> unread count


class MessageView(BaseModel):
    id: str
    from_: str = Field(alias="from")
    to: list[str]
    subject: str | None
    body: str
    content_type: str
    created_at: str
    read_by: list[str]

    model_config = {"populate_by_name": True}


class ThreadDetail(BaseModel):
    thread_id: str
    subject: str | None
    members: list[str]
    messages: list[MessageView]


class SendAs(BaseModel):
    from_: str = Field(alias="from")
    to: str
    body: str
    subject: str | None = None
    in_reply_to: str | None = None

    model_config = {"populate_by_name": True}


class CreateIdentity(BaseModel):
    name: str
```

Add `Field` to the pydantic import at the top of `models.py`:
```python
from pydantic import BaseModel, Field
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_models.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add postbox/models.py tests/test_models.py
git commit -m "observatory: observer pydantic models"
```

---

## Task 3: ObserverService

**Files:** Create `postbox/observer.py`; Test `tests/test_observer.py`

- [ ] **Step 1: Write failing tests** (`tests/test_observer.py`)

```python
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
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/pytest tests/test_observer.py -v`
Expected: FAIL — `postbox.observer` missing.

- [ ] **Step 3: Implement `postbox/observer.py`**

```python
from postbox.agents import AgentService
from postbox.db import Database
from postbox.messages import MessageService
from postbox.models import (AgentFull, CreateIdentity, MessageView, RegisterAgent,
                            SendMessage, ThreadDetail, ThreadSummary)


class ObserverService:
    """Global, identity-agnostic reads + send-as for the web Observatory.
    Reuses AgentService/MessageService; adds unfiltered (all-agents) queries."""

    def __init__(self, db: Database, agents: AgentService, messages: MessageService):
        self.db = db
        self.agents = agents
        self.messages = messages

    async def agents_all(self) -> list[AgentFull]:
        rows = await self.db.fetchall(
            "SELECT id,name,address,profile,status FROM agents ORDER BY "
            "status='offline', address")
        import json
        return [AgentFull(id=r[0], name=r[1], address=r[2],
                          profile=json.loads(r[3]) if r[3] else None, status=r[4])
                for r in rows]

    async def _thread_ids(self, address: str | None) -> list[str]:
        if address is None:
            rows = await self.db.fetchall(
                "SELECT thread_id, MAX(created_at) mx FROM messages "
                "GROUP BY thread_id ORDER BY mx DESC")
            return [r[0] for r in rows]
        agent = await self.agents_get_id(address)
        if agent is None:
            return []
        rows = await self.db.fetchall(
            "SELECT m.thread_id, MAX(m.created_at) mx FROM messages m "
            "WHERE m.sender_id=? OR m.id IN ("
            "  SELECT message_id FROM recipients WHERE agent_id=?) "
            "GROUP BY m.thread_id ORDER BY mx DESC",
            (agent, agent))
        return [r[0] for r in rows]

    async def agents_get_id(self, address: str) -> str | None:
        row = await self.db.fetchone("SELECT id FROM agents WHERE address=?", (address,))
        return row[0] if row else None

    async def _summary(self, tid: str) -> ThreadSummary:
        subj = await self.db.fetchone("SELECT subject FROM messages WHERE id=?", (tid,))
        members = [r[0] for r in await self.db.fetchall(
            "SELECT DISTINCT a.address FROM agents a WHERE a.id IN ("
            "  SELECT sender_id FROM messages WHERE thread_id=? "
            "  UNION SELECT r.agent_id FROM recipients r JOIN messages m "
            "    ON m.id=r.message_id WHERE m.thread_id=?) ORDER BY a.address",
            (tid, tid))]
        last = await self.db.fetchone(
            "SELECT a.address, m.body, m.created_at FROM messages m "
            "JOIN agents a ON a.id=m.sender_id WHERE m.thread_id=? "
            "ORDER BY m.created_at DESC LIMIT 1", (tid,))
        count = (await self.db.fetchone(
            "SELECT COUNT(*) FROM messages WHERE thread_id=?", (tid,)))[0]
        unread = {r[0]: r[1] for r in await self.db.fetchall(
            "SELECT a.address, COUNT(*) FROM recipients r "
            "JOIN messages m ON m.id=r.message_id JOIN agents a ON a.id=r.agent_id "
            "WHERE m.thread_id=? AND r.read_at IS NULL GROUP BY a.address", (tid,))}
        return ThreadSummary(
            thread_id=tid, subject=subj[0] if subj else None, members=members,
            last={"from": last[0], "text": last[1], "at": last[2]} if last else {},
            message_count=count, unread=unread)

    async def list_threads(self, address: str | None = None) -> list[ThreadSummary]:
        return [await self._summary(tid) for tid in await self._thread_ids(address)]

    async def thread(self, thread_id: str) -> ThreadDetail:
        rows = await self.db.fetchall(
            "SELECT m.id, a.address, m.subject, m.body, m.content_type, m.created_at "
            "FROM messages m JOIN agents a ON a.id=m.sender_id "
            "WHERE m.thread_id=? ORDER BY m.created_at", (thread_id,))
        messages = []
        members = set()
        for r in rows:
            recs = await self.db.fetchall(
                "SELECT a.address, r.read_at FROM recipients r "
                "JOIN agents a ON a.id=r.agent_id WHERE r.message_id=?", (r[0],))
            to = [x[0] for x in recs]
            read_by = [x[0] for x in recs if x[1]]
            members.add(r[1]); members.update(to)
            messages.append(MessageView(id=r[0], **{"from": r[1]}, to=to,
                                         subject=r[2], body=r[3], content_type=r[4],
                                         created_at=r[5], read_by=read_by))
        subj = rows[0][2] if rows else None
        return ThreadDetail(thread_id=thread_id, subject=subj,
                            members=sorted(members), messages=messages)

    async def create_identity(self, name: str) -> AgentFull:
        # mark as human so the UI tags it "you"; persists (no MCP session to deregister)
        res = await self.agents.register(RegisterAgent(name=name, profile={"human": True}))
        return AgentFull(id=res.id, name=res.name, address=res.address,
                         profile=res.profile, status="online")

    async def send_as(self, from_address: str, to: str, body: str,
                      subject: str | None = None, in_reply_to: str | None = None):
        sender = await self.agents.get_by_address(from_address)
        if sender is None:
            raise ValueError(f"unknown sender: {from_address}")
        return await self.messages.send(sender.id, SendMessage(
            to=to, body=body, subject=subject, in_reply_to=in_reply_to))
```

> **Note for implementer:** `MessageView(id=..., **{"from": r[1]}, ...)` passes the aliased `from` field; the model has `populate_by_name=True` and `Field(alias="from")`, so both `from` and `from_` work. Verify `test_observer.py::test_thread_detail` passes (it reads `m.from_`).

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_observer.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add postbox/observer.py tests/test_observer.py
git commit -m "observatory: ObserverService (global threads, detail, create-identity, send-as)"
```

---

## Task 4: API observer routes + static UI mount

**Files:** Modify `postbox/api.py`; Test `tests/test_observer_api.py`

- [ ] **Step 1: Write failing tests** (`tests/test_observer_api.py`)

```python
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
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/pytest tests/test_observer_api.py -v`
Expected: FAIL — routes/static missing.

- [ ] **Step 3: Implement in `postbox/api.py`**

Add imports near the top:
```python
import json
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from postbox.observer import ObserverService
from postbox.models import CreateIdentity, SendAs
```

In the lifespan, after `app.state.messages = ...`, add:
```python
        app.state.observer = ObserverService(db, app.state.agents, app.state.messages)
```

Add the routes (place after the existing `/threads/{thread_id}` route, before `/events`):
```python
    @app.get("/observer/agents")
    async def observer_agents():
        return await app.state.observer.agents_all()

    @app.get("/observer/threads")
    async def observer_threads(address: str | None = None):
        return await app.state.observer.list_threads(address)

    @app.get("/observer/threads/{thread_id}")
    async def observer_thread(thread_id: str):
        d = await app.state.observer.thread(thread_id)
        return d.model_dump(by_alias=True)

    @app.post("/observer/identity", status_code=201)
    async def observer_identity(payload: CreateIdentity):
        try:
            return await app.state.observer.create_identity(payload.name)
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/observer/send", status_code=201)
    async def observer_send(payload: SendAs):
        try:
            return await app.state.observer.send_as(
                payload.from_, payload.to, payload.body,
                payload.subject, payload.in_reply_to)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/observer/events")
    async def observer_events(request: Request, last_event_id: int | None = None):
        hdr = request.headers.get("last-event-id")
        start = int(hdr) if hdr else last_event_id
        bus: EventBus = app.state.bus

        async def gen():
            async for ev in bus.stream_all(start):
                yield {"id": str(ev.id), "event": ev.type,
                       "data": json.dumps({**ev.payload, "_id": ev.id, "agent": ev.agent_id})}

        return EventSourceResponse(gen())
```

> **Important:** `observer_threads` returns a list of pydantic models — FastAPI serializes them. But `ThreadDetail`/`MessageView` use `from` aliases, so for `observer_thread` we return `d.model_dump(by_alias=True)` to emit `"from"` (the test asserts `detail["messages"][0]["from"]`). For `observer_threads`, `ThreadSummary` has no alias, so returning the list directly is fine.

After `app = FastAPI(...)` and all routes are defined, mount the static UI at the **end** of `create_app` (just before `return app`):
```python
    web_dir = Path(__file__).parent / "web"
    app.mount("/ui", StaticFiles(directory=str(web_dir), html=True), name="ui")
```

> **Note:** the `web/` directory must exist at import time or `StaticFiles` raises. Task 5 creates it; if running Task 4 alone, create an empty `postbox/web/index.html` first (Task 5 fills it). To keep Task 4 self-contained, add this step:

- [ ] **Step 3b: Create a placeholder `postbox/web/index.html`** so the mount works (Task 5 replaces it):
```html
<!doctype html><html><head><meta charset="utf-8"><title>Postbox</title></head>
<body>Postbox Observatory</body></html>
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_observer_api.py -v`
Expected: 7 passed.

- [ ] **Step 5: Run the FULL suite (no regressions)**

Run: `.venv/bin/pytest -q`
Expected: all pass (existing + new).

- [ ] **Step 6: Commit**

```bash
git add postbox/api.py tests/test_observer_api.py postbox/web/index.html
git commit -m "observatory: /observer REST + firehose SSE + static /ui mount"
```

---

## Task 5: Frontend (Slack Observatory client)

**Files:** Create `postbox/web/index.html`, `postbox/web/styles.css`, `postbox/web/app.js`

This task has no unit tests (no JS test infra); it is verified by serving + screenshot in Task 6. Build the files from the approved mockup `mockups/8-slack-dropdown.html`, splitting CSS/JS out and replacing the static `THREADS`/`IDENTITIES` data with live API calls.

- [ ] **Step 1: Create `postbox/web/styles.css`** — copy the entire `<style>` block contents from `mockups/8-slack-dropdown.html` (the `:root` variables through `.empty`). It is already the approved theme; no changes needed.

- [ ] **Step 2: Create `postbox/web/index.html`** — the markup from `mockups/8-slack-dropdown.html`'s `<body>`, but link the external CSS/JS instead of inline:
```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Postbox — Observatory</title>
<link rel="stylesheet" href="styles.css">
</head>
<body>
<div class="app">
  <div class="side">
    <button class="idswitch" id="idSwitch">
      <span class="logo">📬</span>
      <span class="nm" id="sideName">…</span><span class="car">▾</span>
      <span class="you" id="youTag" style="display:none">you</span>
    </button>
    <div class="idmenu" id="idMenu"></div>
    <div class="search">Search messages…</div>
    <div class="scroller">
      <div class="grp"><span class="car">▾</span> Threads</div>
      <div id="chList"></div>
    </div>
    <div class="foot">Observer · open any identity</div>
  </div>
  <div class="main">
    <div class="mhead">
      <h1 id="mTitle"># </h1>
      <span class="sub" id="mSub"></span>
      <div class="right"><div class="av-row" id="mAvs"></div><span class="obs" id="obsTag" style="display:none"></span></div>
    </div>
    <div class="msgs" id="msgs"></div>
    <div class="composer">
      <div class="cbox">
        <div class="ctools"><b>B</b> <i>i</i> <s>S</s> <span>🔗</span> <span>＠</span> <span>🙂</span> <span>📎</span></div>
        <div class="cinput">
          <input id="cinput" placeholder="Reply…" autocomplete="off">
          <button class="send" id="send" title="Send">➤</button>
        </div>
      </div>
    </div>
  </div>
</div>
<script src="app.js"></script>
</body>
</html>
```

- [ ] **Step 3: Create `postbox/web/app.js`** — live data + SSE. (Color palette is derived deterministically from the address so avatars are stable.)

```javascript
const API = "";  // same origin
const palette = ["#2e8bba","#d9633b","#6b5bd2","#8a4fc4","#2f8f63","#b5495b","#3d7a8c","#9a6a2f"];
const colorFor = a => palette[[...a].reduce((h,c)=>h+c.charCodeAt(0),0) % palette.length];
const initials = n => n.replace(/[^a-z0-9]/gi,'').slice(0,2).toUpperCase();
const esc = s => (s||"").replace(/[&<>]/g, m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]));

let AGENTS = [];                    // [{address,name,status,...}]
let THREADS = [];                   // summaries for the current view
let current = localStorage.getItem("postbox.identity") || "all";
let openThread = null;

const $ = id => document.getElementById(id);
const j = async (u, o) => { const r = await fetch(API+u, o); if(!r.ok) throw new Error(await r.text()); return r.status===204?null:r.json(); };

async function loadAgents(){ AGENTS = await j("/observer/agents"); }
function agentByAddr(a){ return AGENTS.find(x=>x.address===a) || {address:a,name:a,status:"offline"}; }

function totalUnread(t){ return Object.values(t.unread||{}).reduce((a,b)=>a+b,0); }
function unreadForView(t){ return current==="all" ? totalUnread(t) : (t.unread?.[current]||0); }

async function loadThreads(){
  const q = current==="all" ? "" : "?address="+encodeURIComponent(current);
  THREADS = await j("/observer/threads"+q);
}

function renderMenu(){
  const m = $("idMenu"); m.innerHTML = '<div class="mh">Open as</div>';
  const opt = (addr,name,globe,online,you,unread)=>{
    const o = document.createElement("div"); o.className="opt";
    const col = globe ? "#5a2b5c" : colorFor(addr);
    o.innerHTML = `<span class="av ${globe?'globe':''}" style="background:${col}">${globe?'🌐':initials(name)}${globe?'':`<span class="pres ${online?'':'off'}"></span>`}</span>
      <span class="nm">${esc(name)}</span>${you?'<span class="you">you</span>':''}
      ${unread?`<span class="badge">${unread}</span>`:''}${(globe?'all':addr)===current?'<span class="ck">✓</span>':''}`;
    o.onclick = e=>{ e.stopPropagation(); setIdentity(globe?'all':addr); closeMenu(); };
    m.appendChild(o);
  };
  opt(null,"All activity",true,true,false,0);
  const sep = document.createElement("div"); sep.className="sepm"; m.appendChild(sep);
  AGENTS.forEach(a=> opt(a.address, a.name, false, a.status!=="offline", !!a.profile?.human, 0));
  const add = document.createElement("div"); add.className="opt"; add.style.color="#1264a3";
  add.innerHTML = '<span class="av" style="background:#e8eef7;color:#1264a3">＋</span><span class="nm">New identity…</span>';
  add.onclick = async e=>{ e.stopPropagation(); const name = prompt("New identity name (e.g. your name):"); if(name){ const r = await j("/observer/identity",{method:"POST",headers:{'content-type':'application/json'},body:JSON.stringify({name})}); await loadAgents(); setIdentity(r.address); } closeMenu(); };
  m.appendChild(add);
}
function openMenu(){ renderMenu(); $("idMenu").classList.add("show"); $("idSwitch").classList.add("open"); }
function closeMenu(){ $("idMenu").classList.remove("show"); $("idSwitch").classList.remove("open"); }
$("idSwitch").onclick = e=>{ e.stopPropagation(); $("idMenu").classList.contains("show")?closeMenu():openMenu(); };
document.addEventListener("click", closeMenu);

function renderSide(){
  const isAll = current==="all";
  const name = isAll ? "All activity" : (agentByAddr(current).name);
  $("sideName").textContent = name;
  const you = !isAll && !!agentByAddr(current).profile?.human;
  $("youTag").style.display = you ? "inline-block" : "none";
  $("cinput").placeholder = isAll ? "Pick an identity to reply…" : `Reply as ${name}…`;
  $("cinput").disabled = isAll;
  const list = $("chList"); list.innerHTML = "";
  THREADS.forEach(t=>{
    const un = unreadForView(t);
    const others = isAll ? t.members.join(" ↔ ") : t.members.filter(m=>m!==current).join(", ");
    const el = document.createElement("div");
    el.className = "ch"+(openThread===t.thread_id?" active":"")+(un?" unread":"");
    el.innerHTML = `<span class="hash">#</span><span class="name">${esc(t.subject||others||"(no subject)")}</span>`+(un?`<span class="badge">${un}</span>`:"");
    el.title = others;
    el.onclick = ()=> selectThread(t.thread_id);
    list.appendChild(el);
  });
}

async function selectThread(tid){
  openThread = tid;
  const d = await j("/observer/threads/"+tid);
  $("mTitle").textContent = "# "+(d.subject||"(no subject)");
  $("mSub").textContent = d.members.join(" ↔ ");
  const obs = current==="all" || !d.members.includes(current);
  const ot = $("obsTag"); ot.style.display = obs?"inline-block":"none"; ot.textContent = current==="all"?"all activity":"observing";
  const avs = $("mAvs"); avs.innerHTML = "";
  d.members.forEach(mm=>{ const x=document.createElement("div"); x.className="a"; x.style.background=colorFor(mm); x.textContent=initials(mm); avs.appendChild(x); });
  const msgs = $("msgs"); msgs.innerHTML = '<div class="daydiv"><span>Conversation</span></div>';
  d.messages.forEach(m=>{
    const to = m.to[0] || "";
    const isSelf = m.from===current;
    const el = document.createElement("div"); el.className="msg";
    el.innerHTML = `<div class="av" style="background:${colorFor(m.from)}">${initials(m.from)}</div>
      <div><div class="l1"><span class="who">${esc(m.from)}</span>${isSelf?'<span class="self">this identity</span>':''}
      <span class="arrow">→ ${esc(to)}</span><span class="t">${(m.created_at||'').slice(11,16)}</span></div>
      <div class="txt">${esc(m.body)}</div></div>`;
    msgs.appendChild(el);
  });
  msgs.scrollTop = msgs.scrollHeight;
  renderSide();
  if(!current.startsWith("all")) $("cinput").focus();
}

async function setIdentity(idn){
  current = idn; localStorage.setItem("postbox.identity", idn);
  $("sideName").textContent = idn==="all" ? "All activity" : agentByAddr(idn).name;
  await loadThreads();
  openThread = THREADS.length ? THREADS[0].thread_id : null;
  renderSide();
  if(openThread) await selectThread(openThread); else { $("msgs").innerHTML='<div class="empty">No threads</div>'; $("mTitle").textContent="#"; $("mSub").textContent=""; $("mAvs").innerHTML=""; }
}

const input = $("cinput"), sendBtn = $("send");
input.addEventListener("input", ()=> sendBtn.classList.toggle("on", input.value.trim() && current!=="all"));
async function doSend(){
  const txt = input.value.trim(); if(!txt || current==="all" || !openThread) return;
  const t = THREADS.find(x=>x.thread_id===openThread); if(!t) return;
  const to = t.members.find(m=>m!==current) || t.members[0];
  input.value=""; sendBtn.classList.remove("on");
  await j("/observer/send", {method:"POST", headers:{'content-type':'application/json'},
    body: JSON.stringify({from: current, to, body: txt, in_reply_to: lastMsgId(openThread)})});
  await loadThreads(); await selectThread(openThread);
}
let _lastIds = {};
function lastMsgId(tid){ return _lastIds[tid] || null; }
sendBtn.onclick = doSend;
input.addEventListener("keydown", e=>{ if(e.key==="Enter"){ e.preventDefault(); doSend(); }});

function connectLive(){
  const es = new EventSource("/observer/events");
  es.addEventListener("message.received", async ()=>{ await loadThreads(); renderSide(); if(openThread) await selectThread(openThread); });
  es.addEventListener("message.read", async ()=>{ await loadThreads(); renderSide(); });
  es.onerror = ()=>{};  // browser auto-reconnects
}

(async function boot(){
  await loadAgents();
  if(current!=="all" && !AGENTS.some(a=>a.address===current)) current = "all";
  await setIdentity(current);
  connectLive();
})();
```

> **Note for implementer:** `in_reply_to` uses the thread's last message id. The summary doesn't include it, so `lastMsgId` is a best-effort cache; if unset, `send` starts a new thread to the recipient — acceptable for v1 (the simplest correct behavior). A cleaner approach (fold the last message id into `ThreadSummary.last`) is a fine improvement if quick: add `"id": last_id` to `_summary`'s `last` dict and set `_lastIds[tid]` in `selectThread`. Do that if it's clean.

- [ ] **Step 4: Manually verify it loads** (no test): start the server, open `/ui/`:
```bash
.venv/bin/python -m postbox.main &  # then visit http://127.0.0.1:8765/ui/
```
Confirm the page renders, the identity dropdown lists agents, threads load. Stop the server. (Full live proof in Task 6.)

- [ ] **Step 5: Commit**

```bash
git add postbox/web/
git commit -m "observatory: Slack-themed web client (open-as identity, all-activity, reply-as, live SSE)"
```

---

## Task 6: Live e2e proof + docs + final review

**Files:** Create `scripts/observer_e2e.py`; Modify `README.md`, `CLAUDE.md`

- [ ] **Step 1: Create `scripts/observer_e2e.py`**

```python
"""Live proof of the Observatory API: real uvicorn, seed agents + a message,
assert the observer endpoints reflect it and send-as delivers."""
import asyncio, tempfile
import httpx, uvicorn
from postbox.api import create_app

OK = "\033[92mPASS\033[0m"
def check(label, cond): print(f"  [{OK if cond else 'FAIL'}] {label}"); assert cond, label

async def main():
    app = create_app(tempfile.mkdtemp())
    cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="on")
    server = uvicorn.Server(cfg); task = asyncio.create_task(server.serve())
    while not server.started: await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
        a = (await c.post("/agents", json={"name":"alice"})).json()
        b = (await c.post("/agents", json={"name":"bob"})).json()
        await c.post("/messages", headers={"Authorization":f"Bearer {a['token']}"},
                     json={"to":"bob","body":"hi bob","subject":"greeting"})
        print("\n1. OBSERVER sees all threads")
        threads = (await c.get("/observer/threads")).json()
        check("thread visible with both members", any(set(t["members"])=={"alice","bob"} for t in threads))
        print("2. THREAD detail")
        tid = threads[0]["thread_id"]
        d = (await c.get(f"/observer/threads/{tid}")).json()
        check("detail has the message", d["messages"][0]["from"]=="alice")
        print("3. CREATE human identity + SEND AS")
        await c.post("/observer/identity", json={"name":"adam"})
        s = await c.post("/observer/send", json={"from":"adam","to":"alice","body":"ping"})
        check("send-as ok", s.status_code==201)
        check("alice now has a thread with adam", any("adam" in t["members"] for t in (await c.get("/observer/threads", params={"address":"alice"})).json()))
        print("4. UI served")
        check("/ui/ returns html", "Postbox" in (await c.get("/ui/")).text)
    server.should_exit = True; await task
    print("\n\033[92mOBSERVATORY E2E PASSED\033[0m\n")

if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python scripts/observer_e2e.py`
Expected: `OBSERVATORY E2E PASSED`.

- [ ] **Step 3: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all green.

- [ ] **Step 4: Update `README.md`** — add a section:
````markdown
## Web Observatory (human in the loop)
With the server running, open **http://127.0.0.1:8765/ui/** in a browser.
- Click the **name ▾** (top-left) to **open as** any identity, or **All activity** to see every conversation.
- Create your own identity with **"New identity…"** in that dropdown.
- Open a thread to read it; reply as the open identity. Updates stream live.
````

- [ ] **Step 5: Update `CLAUDE.md`** — add `postbox/observer.py`, `postbox/web/`, `scripts/observer_e2e.py`, `tests/test_observer*.py` to the Workspace Index; note the Observatory in Status.

- [ ] **Step 6: Commit**

```bash
git add scripts/observer_e2e.py README.md CLAUDE.md
git commit -m "observatory: live e2e proof + README/index"
```

- [ ] **Step 7: Final whole-feature review** — dispatch a reviewer over the diff: observer queries correct (members/unread aggregation), firehose race-free, send-as reuses delivery+wakeup, no agent-API regressions, static mount safe, frontend escapes user text (XSS).

---

## Done criteria
- `.venv/bin/pytest -q` green (existing + observer tests).
- `scripts/observer_e2e.py` prints PASS.
- `http://127.0.0.1:8765/ui/` renders the Slack Observatory: open-as-any-identity, all-activity, read any thread, reply-as, live updates.
- No changes to the agent-facing REST/SSE/MCP/wakeup behavior.
