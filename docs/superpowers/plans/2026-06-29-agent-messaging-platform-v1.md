# Agent Messaging Platform (Courier) — v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the v1 vertical slice of Courier — a local "email for AI agents": register agents, send threaded messages to a single recipient, durable inbox, read messages, SSE event stream with replay, an MCP server front-end, and a listener daemon that wakes Copilot CLI / the Copilot app on new mail.

**Architecture:** A single FastAPI process backed by SQLite (WAL) is the source of truth. State changes write to SQLite and append to a monotonic `events` log, then publish to an in-process asyncio bus. Clients read mail over REST (the reliable path) and receive live notifications over SSE (best-effort). The MCP server is a thin stdio process that calls the REST API on behalf of one agent; the listener daemon holds the SSE stream and triggers a runtime wakeup on `message.received`.

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, sse-starlette, aiosqlite, Pydantic v2, the official `mcp` SDK, httpx + httpx-sse (client side), pytest + pytest-asyncio.

---

## File Structure

```
pyproject.toml                  # project metadata + deps
README.md                       # run/demo instructions
courier/
  __init__.py
  config.py                     # Settings: data dir, db path, host/port
  db.py                         # aiosqlite connection (WAL), schema init, write helper
  schema.sql                    # DDL for all tables
  models.py                     # Pydantic request/response models
  auth.py                       # token generation/hashing + bearer→agent dependency
  events.py                     # event-log append + in-process bus + SSE replay handoff
  agents.py                     # agent service: register, directory, lookup
  messages.py                   # message service: send, inbox, read, thread
  api.py                        # FastAPI app, REST routes, SSE endpoint
  mcp_server.py                 # MCP stdio server exposing mail tools (calls REST)
  main.py                       # uvicorn entrypoint
  listener/
    __init__.py
    wakeups.py                  # wakeup strategies: copilot_cli, copilot_app, os_notify, stub
    daemon.py                   # SSE client loop → wakeup dispatch
tests/
  conftest.py                   # app + temp-db fixtures, test client, helper to register agents
  test_agents.py
  test_auth.py
  test_messages.py
  test_events.py                # replay/handoff ordering + dedup
  test_sse.py                   # live delivery + reconnect replay
  test_mcp.py
  test_listener.py
```

**Responsibilities (one job each):**
- `db.py` owns the single shared aiosqlite connection and serialized writes — nothing else touches the connection directly.
- `events.py` owns ordering and delivery semantics (the trickiest correctness surface).
- `agents.py` / `messages.py` are pure service logic over `db` + `events`; they never touch FastAPI.
- `api.py` is the only HTTP layer; it calls services.
- `mcp_server.py` and `listener/` are **clients** of the REST API — they do not import the service modules, mirroring real deployment.

---

## Task 0: Project scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `courier/__init__.py` (empty)
- Create: `courier/config.py`
- Create: `tests/conftest.py` (minimal, expanded later)
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "courier"
version = "0.1.0"
description = "Local email-for-AI-agents messaging platform"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.110",
    "uvicorn[standard]>=0.29",
    "sse-starlette>=2.1",
    "aiosqlite>=0.20",
    "pydantic>=2.6",
    "mcp>=1.2",
    "httpx>=0.27",
    "httpx-sse>=0.4",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23", "anyio>=4.0"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

- [ ] **Step 2: Create `courier/config.py`**

```python
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path
    host: str = "127.0.0.1"
    port: int = 8765


def load_settings(data_dir: str | None = None) -> Settings:
    base = Path(data_dir or os.environ.get("COURIER_DATA_DIR", "~/.courier")).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    return Settings(data_dir=base, db_path=base / "courier.db")
```

- [ ] **Step 3: Create `courier/__init__.py` (empty) and write the smoke test `tests/test_smoke.py`**

```python
def test_settings_creates_data_dir(tmp_path):
    from courier.config import load_settings

    s = load_settings(str(tmp_path / "data"))
    assert s.data_dir.exists()
    assert s.db_path.name == "courier.db"
```

- [ ] **Step 4: Install deps and run the smoke test (expect PASS)**

Run: `python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]" && pytest tests/test_smoke.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml courier/__init__.py courier/config.py tests/test_smoke.py
git commit -m "scaffold courier project: config + smoke test"
```

---

## Task 1: Database layer + schema

**Files:**
- Create: `courier/schema.sql`
- Create: `courier/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Create `courier/schema.sql`**

```sql
CREATE TABLE IF NOT EXISTS agents (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  address     TEXT UNIQUE NOT NULL,
  profile     TEXT,
  token_hash  TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id              TEXT PRIMARY KEY,
  thread_id       TEXT NOT NULL,
  in_reply_to     TEXT,
  sender_id       TEXT NOT NULL REFERENCES agents(id),
  subject         TEXT,
  body            TEXT NOT NULL,
  content_type    TEXT NOT NULL DEFAULT 'text/plain',
  idempotency_key TEXT,
  created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_idem
  ON messages(sender_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_messages_thread ON messages(thread_id);

CREATE TABLE IF NOT EXISTS recipients (
  message_id   TEXT NOT NULL REFERENCES messages(id),
  agent_id     TEXT NOT NULL REFERENCES agents(id),
  kind         TEXT NOT NULL,
  delivered_at TEXT,
  read_at      TEXT,
  PRIMARY KEY (message_id, agent_id)
);
CREATE INDEX IF NOT EXISTS ix_recipients_agent ON recipients(agent_id);

CREATE TABLE IF NOT EXISTS attachments (
  id           TEXT PRIMARY KEY,
  message_id   TEXT NOT NULL REFERENCES messages(id),
  filename     TEXT NOT NULL,
  content_type TEXT NOT NULL,
  size         INTEGER NOT NULL,
  blob_path    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id   TEXT NOT NULL REFERENCES agents(id),
  type       TEXT NOT NULL,
  payload    TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_agent_id ON events(agent_id, id);
```

- [ ] **Step 2: Write the failing test `tests/test_db.py`**

```python
import pytest
from courier.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.connect()
    yield d
    await d.close()


async def test_wal_enabled(db):
    row = await db.fetchone("PRAGMA journal_mode;")
    assert row[0].lower() == "wal"


async def test_schema_has_tables(db):
    rows = await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
    )
    names = {r[0] for r in rows}
    assert {"agents", "messages", "recipients", "attachments", "events"} <= names


async def test_execute_and_fetch(db):
    await db.execute(
        "INSERT INTO agents(id,name,address,token_hash,created_at) VALUES (?,?,?,?,?)",
        ("a1", "A", "a", "h", "2026-01-01T00:00:00Z"),
    )
    row = await db.fetchone("SELECT name FROM agents WHERE id=?", ("a1",))
    assert row[0] == "A"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: courier.db`.

- [ ] **Step 4: Implement `courier/db.py`**

```python
import asyncio
from pathlib import Path

import aiosqlite

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


class Database:
    """Single shared aiosqlite connection. All ops run on one background thread,
    so they are inherently serialized; a write lock guards multi-statement writes."""

    def __init__(self, path: Path):
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not connected"
        return self._conn

    @property
    def write_lock(self) -> asyncio.Lock:
        return self._write_lock

    async def execute(self, sql: str, params: tuple = ()) -> None:
        async with self._write_lock:
            await self.conn.execute(sql, params)
            await self.conn.commit()

    async def fetchone(self, sql: str, params: tuple = ()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchall()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_db.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add courier/schema.sql courier/db.py tests/test_db.py
git commit -m "add sqlite database layer (WAL) + schema"
```

---

## Task 2: IDs, time, and auth helpers

**Files:**
- Create: `courier/auth.py`
- Test: `tests/test_auth.py`

- [ ] **Step 1: Write the failing test `tests/test_auth.py`**

```python
from courier.auth import new_id, now_iso, generate_token, hash_token


def test_new_id_unique():
    assert new_id() != new_id()
    assert len(new_id()) >= 16


def test_now_iso_utc():
    assert now_iso().endswith("Z")


def test_token_hash_is_stable_and_matches():
    tok = generate_token()
    assert len(tok) >= 32
    assert hash_token(tok) == hash_token(tok)
    assert hash_token(tok) != hash_token(generate_token())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: courier.auth`.

- [ ] **Step 3: Implement `courier/auth.py`**

```python
import hashlib
import secrets
import uuid
from datetime import datetime, timezone


def new_id() -> str:
    return uuid.uuid4().hex


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_auth.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add courier/auth.py tests/test_auth.py
git commit -m "add id/time/token helpers"
```

---

## Task 3: Models

**Files:**
- Create: `courier/models.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: Write the failing test `tests/test_models.py`**

```python
import pytest
from pydantic import ValidationError
from courier.models import RegisterAgent, SendMessage


def test_register_requires_name_and_address():
    m = RegisterAgent(name="Claude", address="claude")
    assert m.address == "claude"
    with pytest.raises(ValidationError):
        RegisterAgent(name="x")  # missing address


def test_send_message_defaults():
    m = SendMessage(to="cursor", body="hi")
    assert m.content_type == "text/plain"
    assert m.subject is None
    assert m.in_reply_to is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: courier.models`.

- [ ] **Step 3: Implement `courier/models.py`**

```python
from pydantic import BaseModel, Field


class RegisterAgent(BaseModel):
    name: str
    address: str
    profile: dict | None = None


class AgentOut(BaseModel):
    id: str
    name: str
    address: str
    profile: dict | None = None


class RegisterResult(AgentOut):
    token: str


class SendMessage(BaseModel):
    to: str                              # recipient address (v1: single recipient)
    body: str
    subject: str | None = None
    content_type: str = "text/plain"
    in_reply_to: str | None = None
    idempotency_key: str | None = None


class MessageOut(BaseModel):
    id: str
    thread_id: str
    in_reply_to: str | None
    sender: str                          # sender address
    subject: str | None
    body: str
    content_type: str
    created_at: str
    read_at: str | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_models.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add courier/models.py tests/test_models.py
git commit -m "add pydantic models"
```

---

## Task 4: Agent service (register + directory + token lookup)

**Files:**
- Create: `courier/agents.py`
- Test: `tests/test_agents.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Create `tests/conftest.py`**

```python
import pytest
from courier.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "courier.db")
    await d.connect()
    yield d
    await d.close()
```

- [ ] **Step 2: Write the failing test `tests/test_agents.py`**

```python
import pytest
from courier.agents import AgentService
from courier.models import RegisterAgent


async def test_register_returns_token_and_lists_in_directory(db):
    svc = AgentService(db)
    res = await svc.register(RegisterAgent(name="Claude", address="claude"))
    assert res.token
    assert res.address == "claude"

    directory = await svc.directory()
    assert any(a.address == "claude" for a in directory)
    # directory must NOT leak tokens
    assert not hasattr(directory[0], "token")


async def test_duplicate_address_rejected(db):
    svc = AgentService(db)
    await svc.register(RegisterAgent(name="A", address="dup"))
    with pytest.raises(ValueError):
        await svc.register(RegisterAgent(name="B", address="dup"))


async def test_resolve_token(db):
    svc = AgentService(db)
    res = await svc.register(RegisterAgent(name="A", address="a"))
    agent = await svc.resolve_token(res.token)
    assert agent is not None and agent.address == "a"
    assert await svc.resolve_token("bogus") is None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_agents.py -v`
Expected: FAIL — `ModuleNotFoundError: courier.agents`.

- [ ] **Step 4: Implement `courier/agents.py`**

```python
import json

from courier.auth import generate_token, hash_token, new_id, now_iso
from courier.db import Database
from courier.models import AgentOut, RegisterAgent, RegisterResult


class AgentService:
    def __init__(self, db: Database):
        self.db = db

    async def register(self, payload: RegisterAgent) -> RegisterResult:
        existing = await self.db.fetchone(
            "SELECT id FROM agents WHERE address=?", (payload.address,)
        )
        if existing:
            raise ValueError(f"address already registered: {payload.address}")

        token = generate_token()
        agent_id = new_id()
        await self.db.execute(
            "INSERT INTO agents(id,name,address,profile,token_hash,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                agent_id,
                payload.name,
                payload.address,
                json.dumps(payload.profile) if payload.profile else None,
                hash_token(token),
                now_iso(),
            ),
        )
        return RegisterResult(
            id=agent_id, name=payload.name, address=payload.address,
            profile=payload.profile, token=token,
        )

    async def directory(self) -> list[AgentOut]:
        rows = await self.db.fetchall(
            "SELECT id,name,address,profile FROM agents ORDER BY address"
        )
        return [
            AgentOut(
                id=r[0], name=r[1], address=r[2],
                profile=json.loads(r[3]) if r[3] else None,
            )
            for r in rows
        ]

    async def resolve_token(self, token: str) -> AgentOut | None:
        row = await self.db.fetchone(
            "SELECT id,name,address,profile FROM agents WHERE token_hash=?",
            (hash_token(token),),
        )
        if not row:
            return None
        return AgentOut(
            id=row[0], name=row[1], address=row[2],
            profile=json.loads(row[3]) if row[3] else None,
        )

    async def get_by_address(self, address: str) -> AgentOut | None:
        row = await self.db.fetchone(
            "SELECT id,name,address,profile FROM agents WHERE address=?", (address,)
        )
        if not row:
            return None
        return AgentOut(
            id=row[0], name=row[1], address=row[2],
            profile=json.loads(row[3]) if row[3] else None,
        )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_agents.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add courier/agents.py tests/conftest.py tests/test_agents.py
git commit -m "add agent service: register, directory, token lookup"
```

---

## Task 5: Event log + in-process bus + replay handoff

This is the correctness-critical module (spec §8.1). Implement and test ordering/dedup carefully.

**Files:**
- Create: `courier/events.py`
- Test: `tests/test_events.py`

- [ ] **Step 1: Write the failing test `tests/test_events.py`**

```python
import asyncio
import pytest
from courier.events import EventBus, Event


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_events.py -v`
Expected: FAIL — `ModuleNotFoundError: courier.events`.

- [ ] **Step 3: Implement `courier/events.py`**

```python
import asyncio
import json
from dataclasses import dataclass

from courier.auth import now_iso
from courier.db import Database


@dataclass
class Event:
    id: int
    agent_id: str
    type: str
    payload: dict
    created_at: str


class EventBus:
    """Durable event log (SQLite) + in-process pub/sub for SSE.

    Ordering authority is the monotonic events.id. The SSE handoff in `stream`
    subscribes to the live queue FIRST, then replays from the log, then flushes
    the queue while de-duplicating anything already replayed — avoiding the
    gap-drop / duplicate race.
    """

    def __init__(self, db: Database):
        self.db = db
        self._subs: dict[str, set[asyncio.Queue]] = {}

    async def append(self, agent_id: str, type: str, payload: dict) -> Event:
        created = now_iso()
        async with self.db.write_lock:
            cur = await self.db.conn.execute(
                "INSERT INTO events(agent_id,type,payload,created_at) VALUES (?,?,?,?)",
                (agent_id, type, json.dumps(payload), created),
            )
            await self.db.conn.commit()
            event_id = cur.lastrowid
        return Event(event_id, agent_id, type, payload, created)

    async def load_after(self, agent_id: str, after_id: int) -> list[Event]:
        rows = await self.db.fetchall(
            "SELECT id,agent_id,type,payload,created_at FROM events "
            "WHERE agent_id=? AND id>? ORDER BY id",
            (agent_id, after_id),
        )
        return [Event(r[0], r[1], r[2], json.loads(r[3]), r[4]) for r in rows]

    def subscribe(self, agent_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(agent_id, set()).add(q)
        return q

    def unsubscribe(self, agent_id: str, q: asyncio.Queue) -> None:
        subs = self._subs.get(agent_id)
        if subs:
            subs.discard(q)
            if not subs:
                self._subs.pop(agent_id, None)

    async def publish(self, event: Event) -> None:
        for q in list(self._subs.get(event.agent_id, ())):
            await q.put(event)

    async def stream(self, agent_id: str, last_event_id: int | None):
        after = last_event_id or 0
        q = self.subscribe(agent_id)              # (1) live first — buffer concurrent events
        try:
            replayed_max = after
            for ev in await self.load_after(agent_id, after):   # (2) replay backlog
                yield ev
                replayed_max = ev.id
            while True:                            # (3) flush live, dedup <= replayed_max
                ev = await q.get()
                if ev.id <= replayed_max:
                    continue
                yield ev
                replayed_max = ev.id
        finally:
            self.unsubscribe(agent_id, q)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_events.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add courier/events.py tests/test_events.py
git commit -m "add event log + in-process bus with race-free SSE replay handoff"
```

---

## Task 6: Message service (send, inbox, read, thread)

**Files:**
- Create: `courier/messages.py`
- Test: `tests/test_messages.py`

- [ ] **Step 1: Write the failing test `tests/test_messages.py`**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_messages.py -v`
Expected: FAIL — `ModuleNotFoundError: courier.messages`.

- [ ] **Step 3: Implement `courier/messages.py`**

```python
from courier.agents import AgentService
from courier.auth import new_id, now_iso
from courier.db import Database
from courier.events import EventBus
from courier.models import MessageOut, SendMessage


class MessageService:
    def __init__(self, db: Database, agents: AgentService, bus: EventBus):
        self.db = db
        self.agents = agents
        self.bus = bus

    async def _row_to_out(self, row, read_at=None) -> MessageOut:
        sender_addr = (await self.db.fetchone(
            "SELECT address FROM agents WHERE id=?", (row[3],)))[0]
        return MessageOut(
            id=row[0], thread_id=row[1], in_reply_to=row[2], sender=sender_addr,
            subject=row[4], body=row[5], content_type=row[6], created_at=row[8],
            read_at=read_at,
        )

    async def send(self, sender_id: str, payload: SendMessage) -> MessageOut:
        # idempotency: return existing message for a repeated key
        if payload.idempotency_key:
            existing = await self.db.fetchone(
                "SELECT id FROM messages WHERE sender_id=? AND idempotency_key=?",
                (sender_id, payload.idempotency_key),
            )
            if existing:
                return await self.get(sender_id, existing[0])

        recipient = await self.agents.get_by_address(payload.to)
        if recipient is None:
            raise ValueError(f"unknown recipient: {payload.to}")

        msg_id = new_id()
        subject = payload.subject
        thread_id = msg_id
        if payload.in_reply_to:
            parent = await self.db.fetchone(
                "SELECT thread_id,subject FROM messages WHERE id=?",
                (payload.in_reply_to,),
            )
            if parent is None:
                raise ValueError(f"in_reply_to not found: {payload.in_reply_to}")
            thread_id = parent[0]
            if subject is None:
                subject = parent[1]

        created = now_iso()
        async with self.db.write_lock:
            await self.db.conn.execute(
                "INSERT INTO messages(id,thread_id,in_reply_to,sender_id,subject,"
                "body,content_type,idempotency_key,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (msg_id, thread_id, payload.in_reply_to, sender_id, subject,
                 payload.body, payload.content_type, payload.idempotency_key, created),
            )
            await self.db.conn.execute(
                "INSERT INTO recipients(message_id,agent_id,kind,delivered_at) "
                "VALUES (?,?,?,?)",
                (msg_id, recipient.id, "to", created),
            )
            await self.db.conn.commit()

        sender_addr = (await self.db.fetchone(
            "SELECT address FROM agents WHERE id=?", (sender_id,)))[0]
        ev = await self.bus.append(recipient.id, "message.received", {
            "message_id": msg_id, "thread_id": thread_id,
            "from": sender_addr, "subject": subject,
        })
        await self.bus.publish(ev)

        return MessageOut(
            id=msg_id, thread_id=thread_id, in_reply_to=payload.in_reply_to,
            sender=sender_addr, subject=subject, body=payload.body,
            content_type=payload.content_type, created_at=created, read_at=None,
        )

    async def _load(self, agent_id: str, message_id: str, mark_read: bool) -> MessageOut:
        """Participant view. A participant is the sender OR a recipient.
        When mark_read and the caller is an UNREAD recipient, mark it read and
        emit message.read to the sender. The sender viewing their own message
        never marks anything (rec is None for the sender)."""
        row = await self.db.fetchone("SELECT * FROM messages WHERE id=?", (message_id,))
        if row is None:
            raise ValueError("message not found")
        rec = await self.db.fetchone(
            "SELECT read_at FROM recipients WHERE message_id=? AND agent_id=?",
            (message_id, agent_id),
        )
        is_sender = row[3] == agent_id
        if rec is None and not is_sender:
            raise PermissionError("not a participant")
        read_at = rec[0] if rec else None
        if mark_read and rec is not None and rec[0] is None:
            read_at = now_iso()
            await self.db.execute(
                "UPDATE recipients SET read_at=? WHERE message_id=? AND agent_id=?",
                (read_at, message_id, agent_id),
            )
            reader_addr = (await self.db.fetchone(
                "SELECT address FROM agents WHERE id=?", (agent_id,)))[0]
            ev = await self.bus.append(row[3], "message.read",
                                       {"message_id": message_id, "by": reader_addr})
            await self.bus.publish(ev)
        return await self._row_to_out(row, read_at=read_at)

    async def get(self, agent_id: str, message_id: str) -> MessageOut:
        """View without marking read (used internally, e.g. idempotent resend)."""
        return await self._load(agent_id, message_id, mark_read=False)

    async def read(self, agent_id: str, message_id: str) -> MessageOut:
        """View and, for an unread recipient, mark read + emit receipt."""
        return await self._load(agent_id, message_id, mark_read=True)

    async def inbox(self, agent_id: str, unread: bool = False,
                    thread: str | None = None) -> list[MessageOut]:
        sql = (
            "SELECT m.*, r.read_at FROM messages m "
            "JOIN recipients r ON r.message_id=m.id "
            "WHERE r.agent_id=?"
        )
        params: list = [agent_id]
        if unread:
            sql += " AND r.read_at IS NULL"
        if thread:
            sql += " AND m.thread_id=?"
            params.append(thread)
        sql += " ORDER BY m.created_at"
        rows = await self.db.fetchall(sql, tuple(params))
        return [await self._row_to_out(r[:9], read_at=r[9]) for r in rows]

    async def thread(self, agent_id: str, thread_id: str) -> list[MessageOut]:
        rows = await self.db.fetchall(
            "SELECT * FROM messages WHERE thread_id=? ORDER BY created_at", (thread_id,)
        )
        out = []
        for row in rows:
            rec = await self.db.fetchone(
                "SELECT read_at FROM recipients WHERE message_id=? AND agent_id=?",
                (row[0], agent_id),
            )
            if rec is None and row[3] != agent_id:
                continue  # only show messages the agent participates in
            out.append(await self._row_to_out(row, read_at=rec[0] if rec else None))
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_messages.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add courier/messages.py tests/test_messages.py
git commit -m "add message service: send/inbox/read/thread with events + idempotency"
```

---

## Task 7: FastAPI app — REST routes + auth dependency

**Files:**
- Create: `courier/api.py`
- Create: `courier/main.py`
- Test: `tests/test_api.py`

- [ ] **Step 1: Write the failing test `tests/test_api.py`**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_api.py -v`
Expected: FAIL — `ModuleNotFoundError: courier.api`.

- [ ] **Step 3: Implement `courier/api.py`**

```python
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from courier.agents import AgentService
from courier.db import Database
from courier.events import EventBus
from courier.config import load_settings
from courier.messages import MessageService
from courier.models import AgentOut, RegisterAgent, RegisterResult, SendMessage
import json


def create_app(data_dir: str | None = None) -> FastAPI:
    settings = load_settings(data_dir)
    db = Database(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.connect()
        app.state.agents = AgentService(db)
        app.state.bus = EventBus(db)
        app.state.messages = MessageService(db, app.state.agents, app.state.bus)
        yield
        await db.close()

    app = FastAPI(title="Courier", lifespan=lifespan)

    async def current_agent(
        authorization: str = Header(default=""),
    ) -> AgentOut:
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer token")
        token = authorization.removeprefix("Bearer ").strip()
        agent = await app.state.agents.resolve_token(token)
        if agent is None:
            raise HTTPException(401, "invalid token")
        return agent

    @app.post("/agents", status_code=201, response_model=RegisterResult)
    async def register(payload: RegisterAgent):
        try:
            return await app.state.agents.register(payload)
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.get("/agents", response_model=list[AgentOut])
    async def directory():
        return await app.state.agents.directory()

    @app.post("/messages", status_code=201)
    async def send(payload: SendMessage, agent: AgentOut = Depends(current_agent)):
        try:
            return await app.state.messages.send(agent.id, payload)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/inbox")
    async def inbox(unread: bool = False, thread: str | None = None,
                    agent: AgentOut = Depends(current_agent)):
        return await app.state.messages.inbox(agent.id, unread=unread, thread=thread)

    @app.get("/messages/{message_id}")
    async def read_message(message_id: str, agent: AgentOut = Depends(current_agent)):
        try:
            return await app.state.messages.read(agent.id, message_id)
        except PermissionError as e:
            raise HTTPException(403, str(e))
        except ValueError as e:
            raise HTTPException(404, str(e))

    @app.get("/threads/{thread_id}")
    async def thread(thread_id: str, agent: AgentOut = Depends(current_agent)):
        return await app.state.messages.thread(agent.id, thread_id)

    @app.get("/events")
    async def events(request: Request, last_event_id: int | None = None,
                     agent: AgentOut = Depends(current_agent)):
        # honor Last-Event-ID header if present
        hdr = request.headers.get("last-event-id")
        start = int(hdr) if hdr else last_event_id
        bus: EventBus = app.state.bus

        async def gen():
            async for ev in bus.stream(agent.id, start):
                yield {"id": str(ev.id), "event": ev.type,
                       "data": json.dumps({**ev.payload, "_id": ev.id})}

        return EventSourceResponse(gen())

    return app
```

- [ ] **Step 4: Implement `courier/main.py`**

```python
import uvicorn

from courier.api import create_app
from courier.config import load_settings

app = create_app()

if __name__ == "__main__":
    s = load_settings()
    uvicorn.run(app, host=s.host, port=s.port)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_api.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add courier/api.py courier/main.py tests/test_api.py
git commit -m "add FastAPI app: REST routes, bearer auth, SSE endpoint"
```

---

## Task 8: SSE end-to-end (live delivery + reconnect replay)

**Files:**
- Test: `tests/test_sse.py`

This task adds no new production code — it proves the SSE endpoint end-to-end through the HTTP layer using `httpx-sse`.

- [ ] **Step 1: Write the failing test `tests/test_sse.py`**

```python
import asyncio
import json
import pytest
from httpx import ASGITransport, AsyncClient
from httpx_sse import aconnect_sse
from courier.api import create_app


@pytest.fixture
async def app_client(tmp_path):
    app = create_app(str(tmp_path / "data"))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            yield app, c


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
```

- [ ] **Step 2: Run tests to verify they pass (production code already exists)**

Run: `pytest tests/test_sse.py -v`
Expected: 2 passed. If `test_live_event_delivered_over_sse` hangs, verify `EventSourceResponse` is streaming and `bus.stream` subscribes before replay.

- [ ] **Step 3: Commit**

```bash
git add tests/test_sse.py
git commit -m "test SSE end-to-end: live delivery + reconnect replay"
```

---

## Task 9: MCP server (agent-facing tools over REST)

The MCP server runs as a separate stdio process launched by each agent runtime. It reads `COURIER_URL` and `COURIER_TOKEN` from the environment and calls the REST API. It does **not** import the service modules.

**Files:**
- Create: `courier/mcp_server.py`
- Test: `tests/test_mcp.py`

- [ ] **Step 1: Write the failing test `tests/test_mcp.py`**

The tools are thin wrappers; test the wrapper functions directly against a running ASGI app via an injected httpx client.

```python
import pytest
from httpx import ASGITransport, AsyncClient
from courier.api import create_app
from courier.mcp_server import MailTools


@pytest.fixture
async def tools(tmp_path):
    app = create_app(str(tmp_path / "data"))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            a = (await c.post("/agents", json={"name": "A", "address": "a"})).json()
            b = (await c.post("/agents", json={"name": "B", "address": "b"})).json()
            yield MailTools(c, a["token"]), MailTools(c, b["token"])


async def test_list_agents_tool(tools):
    a_mail, _ = tools
    agents = await a_mail.list_agents()
    assert {x["address"] for x in agents} == {"a", "b"}


async def test_send_then_recipient_sees_in_inbox(tools):
    a_mail, b_mail = tools
    await a_mail.send_message(to="b", body="hi", subject="s")
    inbox = await b_mail.check_inbox(unread=True)
    assert [m["body"] for m in inbox] == ["hi"]


async def test_reply_threads_and_routes_back(tools):
    a_mail, b_mail = tools
    m = await a_mail.send_message(to="b", body="q", subject="Q")
    r = await b_mail.reply(message_id=m["id"], body="re")  # B reads original, replies to A
    assert r["thread_id"] == m["thread_id"]
    a_inbox = await a_mail.check_inbox(unread=True)
    assert [x["body"] for x in a_inbox] == ["re"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mcp.py -v`
Expected: FAIL — `ModuleNotFoundError: courier.mcp_server`.

- [ ] **Step 3: Implement `courier/mcp_server.py`**

```python
import os

import httpx
from mcp.server.fastmcp import FastMCP


class MailTools:
    """Thin REST client used by the MCP tools (and unit tests)."""

    def __init__(self, client: httpx.AsyncClient, token: str):
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}"}

    async def list_agents(self) -> list[dict]:
        r = await self.client.get("/agents")
        r.raise_for_status()
        return r.json()

    async def send_message(self, to: str, body: str, subject: str | None = None,
                           content_type: str = "text/plain",
                           in_reply_to: str | None = None) -> dict:
        r = await self.client.post("/messages", headers=self.headers, json={
            "to": to, "body": body, "subject": subject,
            "content_type": content_type, "in_reply_to": in_reply_to,
        })
        r.raise_for_status()
        return r.json()

    async def check_inbox(self, unread: bool = True, thread: str | None = None) -> list[dict]:
        params = {"unread": unread}
        if thread:
            params["thread"] = thread
        r = await self.client.get("/inbox", headers=self.headers, params=params)
        r.raise_for_status()
        return r.json()

    async def read_message(self, message_id: str) -> dict:
        r = await self.client.get(f"/messages/{message_id}", headers=self.headers)
        r.raise_for_status()
        return r.json()

    async def reply(self, message_id: str, body: str,
                    content_type: str = "text/plain") -> dict:
        # fetch the original to find sender + thread, then send a reply to the sender
        original = await self.read_message(message_id)
        return await self.send_message(
            to=original["sender"], body=body, content_type=content_type,
            in_reply_to=message_id,
        )


def build_server() -> FastMCP:
    url = os.environ.get("COURIER_URL", "http://127.0.0.1:8765")
    token = os.environ["COURIER_TOKEN"]
    client = httpx.AsyncClient(base_url=url)
    tools = MailTools(client, token)

    mcp = FastMCP("courier-mail")

    @mcp.tool()
    async def list_agents() -> list[dict]:
        """List all agents you can message (the directory)."""
        return await tools.list_agents()

    @mcp.tool()
    async def send_message(to: str, body: str, subject: str = "",
                           in_reply_to: str = "") -> dict:
        """Send a message to another agent by address."""
        return await tools.send_message(
            to=to, body=body, subject=subject or None,
            in_reply_to=in_reply_to or None,
        )

    @mcp.tool()
    async def check_inbox(unread: bool = True) -> list[dict]:
        """List messages in your inbox. unread=True shows only unread."""
        return await tools.check_inbox(unread=unread)

    @mcp.tool()
    async def read_message(message_id: str) -> dict:
        """Read a message by id (marks it read)."""
        return await tools.read_message(message_id)

    @mcp.tool()
    async def reply(message_id: str, body: str) -> dict:
        """Reply to a message, keeping it in the same thread."""
        return await tools.reply(message_id, body)

    return mcp


if __name__ == "__main__":
    build_server().run()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_mcp.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add courier/mcp_server.py tests/test_mcp.py
git commit -m "add MCP server exposing mail tools over REST"
```

---

## Task 10: Listener daemon + wakeup strategies

**Files:**
- Create: `courier/listener/__init__.py` (empty)
- Create: `courier/listener/wakeups.py`
- Create: `courier/listener/daemon.py`
- Test: `tests/test_listener.py`

- [ ] **Step 1: Write the failing test `tests/test_listener.py`**

```python
import pytest
from courier.listener.wakeups import StubWakeup, build_wakeup


async def test_stub_wakeup_records_events():
    w = StubWakeup()
    await w.wake({"from": "a", "subject": "hi", "message_id": "m1"})
    assert w.calls == [{"from": "a", "subject": "hi", "message_id": "m1"}]


def test_build_wakeup_selects_strategy():
    assert build_wakeup("stub").__class__.__name__ == "StubWakeup"
    assert build_wakeup("copilot_cli").__class__.__name__ == "CopilotCliWakeup"
    assert build_wakeup("copilot_app").__class__.__name__ == "CopilotAppWakeup"
    with pytest.raises(ValueError):
        build_wakeup("nonsense")


async def test_copilot_cli_builds_command(monkeypatch):
    from courier.listener.wakeups import CopilotCliWakeup
    captured = {}

    async def fake_run(cmd):
        captured["cmd"] = cmd

    w = CopilotCliWakeup(runner=fake_run)
    await w.wake({"from": "cursor", "subject": "Review", "message_id": "m9"})
    assert captured["cmd"][0] == "copilot"
    assert "-p" in captured["cmd"]
    assert any("cursor" in part for part in captured["cmd"])


async def test_copilot_app_builds_deeplink(monkeypatch):
    from courier.listener.wakeups import CopilotAppWakeup
    captured = {}

    async def fake_run(cmd):
        captured["cmd"] = cmd

    w = CopilotAppWakeup(repo="me/repo", runner=fake_run)
    await w.wake({"from": "cli", "subject": "Hi", "message_id": "m1"})
    link = captured["cmd"][-1]
    assert link.startswith("ghapp://session/new?")
    assert "repo=me%2Frepo" in link
    assert "prompt=" in link
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_listener.py -v`
Expected: FAIL — `ModuleNotFoundError: courier.listener.wakeups`.

- [ ] **Step 3: Implement `courier/listener/wakeups.py`**

```python
import asyncio
import shlex
from urllib.parse import quote, urlencode


def _notification_text(event: dict) -> str:
    subj = event.get("subject") or "(no subject)"
    return (f"📬 New mail from {event.get('from')}: \"{subj}\" "
            f"(message {event.get('message_id')}). "
            f"Use your mail tools to check_inbox and read_message, then reply.")


async def _default_runner(cmd: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(*cmd)
    await proc.wait()


class StubWakeup:
    """Used in tests and for dry runs — records calls instead of spawning."""

    def __init__(self):
        self.calls: list[dict] = []

    async def wake(self, event: dict) -> None:
        self.calls.append(event)


class CopilotCliWakeup:
    def __init__(self, runner=_default_runner):
        self._run = runner

    async def wake(self, event: dict) -> None:
        await self._run(["copilot", "-p", _notification_text(event)])


class CopilotAppWakeup:
    def __init__(self, repo: str, runner=_default_runner):
        self.repo = repo
        self._run = runner

    async def wake(self, event: dict) -> None:
        query = urlencode({"repo": self.repo, "mode": "interactive",
                           "prompt": _notification_text(event)}, quote_via=quote)
        link = f"ghapp://session/new?{query}"
        # macOS opens custom URL schemes via `open`
        await self._run(["open", link])


class OsNotifyWakeup:
    def __init__(self, runner=_default_runner):
        self._run = runner

    async def wake(self, event: dict) -> None:
        text = _notification_text(event)
        script = f'display notification {shlex.quote(text)} with title "Courier"'
        await self._run(["osascript", "-e", script])


def build_wakeup(kind: str, repo: str = "owner/repo"):
    if kind == "stub":
        return StubWakeup()
    if kind == "copilot_cli":
        return CopilotCliWakeup()
    if kind == "copilot_app":
        return CopilotAppWakeup(repo=repo)
    if kind == "os_notify":
        return OsNotifyWakeup()
    raise ValueError(f"unknown wakeup strategy: {kind}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_listener.py -v`
Expected: 5 passed.

- [ ] **Step 5: Implement `courier/listener/daemon.py`**

```python
import argparse
import asyncio
import json
import os

import httpx
from httpx_sse import aconnect_sse

from courier.listener.wakeups import build_wakeup


async def run_daemon(url: str, token: str, wakeup) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    last_id = "0"
    async with httpx.AsyncClient(base_url=url, timeout=None) as client:
        while True:
            try:
                async with aconnect_sse(
                    client, "GET", "/events",
                    headers={**headers, "Last-Event-ID": last_id},
                ) as es:
                    async for sse in es.aiter_sse():
                        last_id = sse.id or last_id
                        if sse.event == "message.received":
                            await wakeup.wake(json.loads(sse.data))
            except (httpx.HTTPError, httpx.TransportError):
                await asyncio.sleep(1)  # reconnect with backoff


def main() -> None:
    p = argparse.ArgumentParser(description="Courier listener daemon")
    p.add_argument("--url", default=os.environ.get("COURIER_URL", "http://127.0.0.1:8765"))
    p.add_argument("--token", default=os.environ.get("COURIER_TOKEN"))
    p.add_argument("--wakeup", default="os_notify",
                   choices=["stub", "copilot_cli", "copilot_app", "os_notify"])
    p.add_argument("--repo", default="owner/repo", help="repo for copilot_app deep link")
    args = p.parse_args()
    if not args.token:
        raise SystemExit("COURIER_TOKEN (or --token) is required")
    wakeup = build_wakeup(args.wakeup, repo=args.repo)
    asyncio.run(run_daemon(args.url, args.token, wakeup))


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run the listener tests again (daemon has no new unit test; verified in Task 11 demo)**

Run: `pytest tests/test_listener.py -v`
Expected: 5 passed.

- [ ] **Step 7: Commit**

```bash
git add courier/listener/ tests/test_listener.py
git commit -m "add listener daemon + wakeup strategies (copilot cli/app, os-notify, stub)"
```

---

## Task 11: README + end-to-end manual demo

**Files:**
- Create: `README.md`
- Update: `CLAUDE.md` (workspace index)

- [ ] **Step 1: Write `README.md`**

````markdown
# Courier — local email for AI agents

Each agent has an identity + inbox and exchanges async, threaded messages.

## Run the service
```bash
pip install -e ".[dev]"
python -m courier.main          # serves http://127.0.0.1:8765
```

## Register two agents
```bash
curl -s -XPOST localhost:8765/agents -d '{"name":"Copilot CLI","address":"copilot"}' -H 'content-type: application/json'
curl -s -XPOST localhost:8765/agents -d '{"name":"Copilot App","address":"app"}'      -H 'content-type: application/json'
# each returns a one-time token
```

## Wire up MCP (both Copilot surfaces share one config)
`~/.copilot/mcp-config.json`:
```json
{
  "mcpServers": {
    "courier": {
      "type": "local",
      "command": "python",
      "args": ["-m", "courier.mcp_server"],
      "env": { "COURIER_URL": "http://127.0.0.1:8765", "COURIER_TOKEN": "<copilot-token>" }
    }
  }
}
```
The standalone Copilot app auto-inherits this server.

## Run a listener (wakeup on new mail)
```bash
COURIER_TOKEN=<app-token> python -m courier.listener.daemon --wakeup copilot_app --repo owner/repo
# or --wakeup copilot_cli  /  --wakeup os_notify  /  --wakeup stub
```

## Manual end-to-end check
1. Start the service.
2. Start a listener for `app` with `--wakeup stub` in one terminal — leave it running.
3. Send a message as `copilot` to `app`:
   ```bash
   curl -s -XPOST localhost:8765/messages -H "Authorization: Bearer <copilot-token>" \
     -H 'content-type: application/json' -d '{"to":"app","body":"can you review PR #42?","subject":"review"}'
   ```
4. Confirm the listener logged the wakeup, and the message is in `app`'s inbox:
   ```bash
   curl -s localhost:8765/inbox -H "Authorization: Bearer <app-token>"
   ```
````

- [ ] **Step 2: Update `CLAUDE.md` workspace index**

Replace the "Status" line and add the code modules to the Workspace Index section (service implemented; list `courier/` modules and `tests/`).

- [ ] **Step 3: Run the full test suite**

Run: `pytest -v`
Expected: all tests pass (Tasks 0–10).

- [ ] **Step 4: Manual smoke per README steps 1–4**

Run the service, send a message via curl, confirm inbox + stub listener wakeup. Capture output as evidence.

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "add README + e2e demo instructions; update workspace index"
```

---

## Done criteria (v1)
- `pytest -v` green across all tasks.
- Two agents can register, message (threaded), read, and the recipient is woken via the listener (stub verified; copilot_cli/app commands built correctly).
- One MCP config serves both Copilot CLI and the Copilot app.
- Inbox is durable: an agent that was never connected to SSE still sees mail via `GET /inbox`.
