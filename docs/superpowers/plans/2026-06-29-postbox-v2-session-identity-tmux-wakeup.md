# Postbox v2 — Session Identity + Real-Time tmux Wakeup — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make Postbox identity per-session and self-assigned (token-less shared MCP config), and deliver messages to a recipient **in real time by injecting into its tmux pane** — so copilot1 → copilot2 lands instantly whether copilot2 is idle or busy, with no manual "check your inbox."

**Architecture:** Each Copilot session spawns its own `postbox.mcp_server` stdio process. That process (1) **auto-registers** an identity on startup, capturing its inherited `$TMUX_PANE` as its wakeup target, (2) runs a **background SSE loop** that, on `message.received`, does `tmux send-keys` into its own pane, and (3) **deregisters** on shutdown. The shared `mcp-config.json` carries only `COURIER_URL` — no token. Built on v1 (REST+SSE+SQLite+inbox+events), all of which is reused unchanged except the agent/identity surface.

**Tech Stack:** Python/FastAPI/SQLite/aiosqlite/`mcp` SDK (`FastMCP` with verified `instructions=` + `lifespan=`)/httpx+httpx-sse/`tmux` binary at runtime.

**Verified API facts (do not re-derive):**
- `FastMCP(name, instructions=..., lifespan=...)` — both params exist (mcp SDK installed here). `lifespan` is an `@asynccontextmanager async def lifespan(server): ... yield state`.
- `tmux send-keys -l -t <pane> "<text>"` then `tmux send-keys -t <pane> Enter` delivers `<text>\n` to the program running in `<pane>` (confirmed: a `cat` in the pane received it).
- A child process launched in a tmux pane inherits `$TMUX_PANE` (confirmed).

---

## File Structure

```
postbox/schema.sql        MODIFY  add columns to agents (wakeup_kind, wakeup_target, status, last_seen)
postbox/db.py             MODIFY  additive migration for existing DBs (ALTER TABLE if column missing)
postbox/models.py         MODIFY  Wakeup model; RegisterAgent +wakeup/name-optional; AgentOut +status; SetName
postbox/agents.py         MODIFY  register stores wakeup+status; set_name; deregister; set_status; directory online-only
postbox/api.py            MODIFY  register accepts wakeup; PATCH/DELETE /agents/self; presence via SSE connect/disconnect
postbox/listener/wakeups.py MODIFY  add TmuxWakeup + build_wakeup('tmux', target=...)
postbox/mcp_server.py     MODIFY  v2: Session (auto-register + background SSE wakeup + deregister), lifespan, set_name tool, instructions
README.md                 MODIFY  token-less shared config + two-pane tmux workflow
CLAUDE.md                 MODIFY  index/status
scripts/v2_tmux_e2e.py    CREATE  real-tmux end-to-end proof (capture-pane)
tests/test_models.py      MODIFY  v2 model tests
tests/test_agents.py      MODIFY  set_name/deregister/status/online-directory tests
tests/test_api.py         MODIFY  register-with-wakeup, PATCH/DELETE self, presence tests
tests/test_listener.py    MODIFY  TmuxWakeup unit + real-tmux capture test
tests/test_mcp.py         MODIFY  v2 Session auto-register + wakeup-loop tests
```

**Identity model decided (from spec §8):** `id` = session uuid (already how v1 mints ids). `address` = the **addressable handle = display name** (default `copilot-<short-id>`, changed by `set_name`, UNIQUE). `name` column kept in sync with `address`. This keeps `messages.py` (resolves recipients by `address`) unchanged.

---

## Task 1: Schema + additive migration

**Files:** Modify `postbox/schema.sql`, `postbox/db.py`; Test `tests/test_db.py`

- [ ] **Step 1: Add columns to `agents` in `postbox/schema.sql`** (append the new columns to the CREATE TABLE; for a fresh DB they're created directly)

Change the `agents` table definition to:
```sql
CREATE TABLE IF NOT EXISTS agents (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  address       TEXT UNIQUE NOT NULL,
  profile       TEXT,
  token_hash    TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  wakeup_kind   TEXT NOT NULL DEFAULT 'none',
  wakeup_target TEXT,
  status        TEXT NOT NULL DEFAULT 'online',
  last_seen     TEXT
);
```

- [ ] **Step 2: Write the failing test in `tests/test_db.py`** (append)

```python
async def test_agents_has_v2_columns(db):
    rows = await db.fetchall("PRAGMA table_info(agents);")
    cols = {r[1] for r in rows}
    assert {"wakeup_kind", "wakeup_target", "status", "last_seen"} <= cols
```

- [ ] **Step 3: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_db.py::test_agents_has_v2_columns -v`
Expected: FAIL on a pre-existing DB without the columns (or PASS on a fresh temp DB). To make this robust for **existing** DBs, add the migration in Step 4.

- [ ] **Step 4: Add an additive migration in `postbox/db.py`** so existing `~/.postbox/postbox.db` files gain the columns. In `connect()`, after `executescript(SCHEMA)` and before `commit()`, add:

```python
        await self._migrate()
```

And add the method:
```python
    async def _migrate(self) -> None:
        """Additive, idempotent migrations for existing databases."""
        cur = await self._conn.execute("PRAGMA table_info(agents);")
        cols = {r[1] for r in await cur.fetchall()}
        adds = {
            "wakeup_kind": "TEXT NOT NULL DEFAULT 'none'",
            "wakeup_target": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'online'",
            "last_seen": "TEXT",
        }
        for col, decl in adds.items():
            if col not in cols:
                await self._conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {decl};")
        # pre-existing rows have no live SSE session — don't show them online
        if "status" not in cols:
            await self._conn.execute("UPDATE agents SET status='offline';")
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: all pass (existing 3 + new 1 = 4).

- [ ] **Step 6: Commit**

```bash
git add postbox/schema.sql postbox/db.py tests/test_db.py
git commit -m "v2: add wakeup/status/last_seen columns to agents + additive migration"
```

---

## Task 2: Models v2

**Files:** Modify `postbox/models.py`; Test `tests/test_models.py`

- [ ] **Step 1: Write failing tests in `tests/test_models.py`** (append)

```python
def test_wakeup_model_and_register_defaults():
    from postbox.models import RegisterAgent, Wakeup
    m = RegisterAgent(wakeup=Wakeup(kind="tmux", target="%5"))
    assert m.name is None                 # name optional in v2 (server defaults it)
    assert m.wakeup.kind == "tmux" and m.wakeup.target == "%5"
    m2 = RegisterAgent()
    assert m2.wakeup.kind == "none"       # default wakeup


def test_set_name_model():
    from postbox.models import SetName
    assert SetName(name="alice").name == "alice"
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/pytest tests/test_models.py -v`
Expected: FAIL — `Wakeup`/`SetName` undefined, `RegisterAgent.name` currently required.

- [ ] **Step 3: Implement in `postbox/models.py`**

Add near the top (after imports):
```python
class Wakeup(BaseModel):
    kind: str = "none"          # 'tmux' | 'os_notify' | 'none'
    target: str | None = None   # e.g. the $TMUX_PANE value
```

Change `RegisterAgent` to:
```python
class RegisterAgent(BaseModel):
    name: str | None = None     # v2: optional; server defaults to copilot-<short id>
    address: str | None = None  # v2: optional; defaults to name
    profile: dict | None = None
    wakeup: Wakeup = Wakeup()
```

Add `status` to `AgentOut`:
```python
class AgentOut(BaseModel):
    id: str
    name: str
    address: str
    profile: dict | None = None
    status: str = "online"
```

Add:
```python
class SetName(BaseModel):
    name: str
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_models.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add postbox/models.py tests/test_models.py
git commit -m "v2: Wakeup model, optional name on register, status on AgentOut, SetName"
```

---

## Task 3: Agent service v2 (register+wakeup, set_name, deregister, presence, online directory)

**Files:** Modify `postbox/agents.py`; Test `tests/test_agents.py`

- [ ] **Step 1: Write failing tests in `tests/test_agents.py`** (append)

```python
import pytest
from postbox.models import Wakeup


async def test_register_defaults_name_and_stores_wakeup(db):
    from postbox.agents import AgentService
    from postbox.models import RegisterAgent
    svc = AgentService(db)
    res = await svc.register(RegisterAgent(wakeup=Wakeup(kind="tmux", target="%9")))
    assert res.address.startswith("copilot-")      # defaulted handle
    row = await db.fetchone(
        "SELECT wakeup_kind,wakeup_target,status FROM agents WHERE id=?", (res.id,))
    assert row == ("tmux", "%9", "online")


async def test_set_name_changes_handle_and_rejects_duplicate(db):
    from postbox.agents import AgentService
    from postbox.models import RegisterAgent
    svc = AgentService(db)
    a = await svc.register(RegisterAgent())
    b = await svc.register(RegisterAgent())
    renamed = await svc.set_name(a.id, "alice")
    assert renamed.name == "alice" and renamed.address == "alice"
    assert await svc.get_by_address("alice") is not None
    with pytest.raises(ValueError):
        await svc.set_name(b.id, "alice")              # taken


async def test_deregister_and_online_directory(db):
    from postbox.agents import AgentService
    from postbox.models import RegisterAgent
    svc = AgentService(db)
    a = await svc.register(RegisterAgent())
    b = await svc.register(RegisterAgent())
    await svc.set_status(b.id, "offline")
    online = await svc.directory()                      # online-only by default
    ids = {x.id for x in online}
    assert a.id in ids and b.id not in ids
    await svc.deregister(a.id)
    assert a.id not in {x.id for x in await svc.directory()}
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/pytest tests/test_agents.py -v`
Expected: FAIL — `set_name`/`deregister`/`set_status` undefined; register doesn't accept wakeup/default name.

- [ ] **Step 3: Implement in `postbox/agents.py`**

Replace the `register` method and add the new methods. New `register`:
```python
    async def register(self, payload: RegisterAgent) -> RegisterResult:
        agent_id = new_id()
        name = payload.name or f"copilot-{agent_id[:8]}"
        address = payload.address or name
        existing = await self.db.fetchone(
            "SELECT id FROM agents WHERE address=?", (address,))
        if existing:
            raise ValueError(f"address already registered: {address}")
        token = generate_token()
        now = now_iso()
        await self.db.execute(
            "INSERT INTO agents(id,name,address,profile,token_hash,created_at,"
            "wakeup_kind,wakeup_target,status,last_seen) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (agent_id, name, address,
             json.dumps(payload.profile) if payload.profile else None,
             hash_token(token), now,
             payload.wakeup.kind, payload.wakeup.target, "online", now),
        )
        return RegisterResult(id=agent_id, name=name, address=address,
                              profile=payload.profile, token=token)

    async def set_name(self, agent_id: str, name: str) -> AgentOut:
        taken = await self.db.fetchone(
            "SELECT id FROM agents WHERE address=? AND id<>?", (name, agent_id))
        if taken:
            raise ValueError(f"name already taken: {name}")
        await self.db.execute(
            "UPDATE agents SET name=?, address=? WHERE id=?", (name, name, agent_id))
        return await self._get(agent_id)

    async def set_status(self, agent_id: str, status: str) -> None:
        await self.db.execute(
            "UPDATE agents SET status=?, last_seen=? WHERE id=?",
            (status, now_iso(), agent_id))

    async def deregister(self, agent_id: str) -> None:
        await self.db.execute("UPDATE agents SET status='offline' WHERE id=?", (agent_id,))

    async def _get(self, agent_id: str) -> AgentOut:
        r = await self.db.fetchone(
            "SELECT id,name,address,profile,status FROM agents WHERE id=?", (agent_id,))
        return AgentOut(id=r[0], name=r[1], address=r[2],
                        profile=json.loads(r[3]) if r[3] else None, status=r[4])
```

Change `directory` to return online agents and include status:
```python
    async def directory(self, include_offline: bool = False) -> list[AgentOut]:
        sql = "SELECT id,name,address,profile,status FROM agents"
        if not include_offline:
            sql += " WHERE status='online'"
        sql += " ORDER BY address"
        rows = await self.db.fetchall(sql)
        return [AgentOut(id=r[0], name=r[1], address=r[2],
                         profile=json.loads(r[3]) if r[3] else None, status=r[4])
                for r in rows]
```

Update `resolve_token` and `get_by_address` to also select+include `status` (so `AgentOut` always has it). For `resolve_token`:
```python
    async def resolve_token(self, token: str) -> AgentOut | None:
        row = await self.db.fetchone(
            "SELECT id,name,address,profile,status FROM agents WHERE token_hash=?",
            (hash_token(token),))
        if not row:
            return None
        return AgentOut(id=row[0], name=row[1], address=row[2],
                        profile=json.loads(row[3]) if row[3] else None, status=row[4])
```
And `get_by_address` identically (add `,status` to the SELECT and `status=row[4]` to the AgentOut). Deregister is "soft" (status=offline) so message history/inbox stays intact.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_agents.py -v`
Expected: all pass (existing + 3 new).

- [ ] **Step 5: Commit**

```bash
git add postbox/agents.py tests/test_agents.py
git commit -m "v2: agent service — default handle, wakeup storage, set_name, presence, online directory"
```

---

## Task 4: API v2 (wakeup on register, set_name, deregister, SSE presence)

**Files:** Modify `postbox/api.py`; Test `tests/test_api.py`

- [ ] **Step 1: Write failing tests in `tests/test_api.py`** (append; reuse the existing `client` fixture)

```python
async def test_register_with_wakeup_and_default_name(client):
    r = await client.post("/agents", json={"wakeup": {"kind": "tmux", "target": "%3"}})
    assert r.status_code == 201
    body = r.json()
    assert body["address"].startswith("copilot-") and body["token"]


async def test_set_name_then_address_by_name(client):
    a = (await client.post("/agents", json={})).json()
    ah = {"Authorization": f"Bearer {a['token']}"}
    r = await client.patch("/agents/self", headers=ah, json={"name": "alice"})
    assert r.status_code == 200 and r.json()["address"] == "alice"
    # another agent can now send to "alice"
    b = (await client.post("/agents", json={})).json()
    bh = {"Authorization": f"Bearer {b['token']}"}
    s = await client.post("/messages", headers=bh, json={"to": "alice", "body": "hi"})
    assert s.status_code == 201


async def test_deregister_self(client):
    a = (await client.post("/agents", json={})).json()
    ah = {"Authorization": f"Bearer {a['token']}"}
    r = await client.delete("/agents/self", headers=ah)
    assert r.status_code == 204
    # no longer in the online directory
    assert all(x["id"] != a["id"] for x in (await client.get("/agents")).json())
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/pytest tests/test_api.py -v`
Expected: FAIL — PATCH/DELETE routes missing.

- [ ] **Step 3: Implement in `postbox/api.py`**

Add `SetName` to the models import:
```python
from postbox.models import AgentOut, RegisterAgent, RegisterResult, SendMessage, SetName
```

Add routes after the existing `/agents` routes:
```python
    @app.patch("/agents/self", response_model=AgentOut)
    async def set_name(payload: SetName, agent: AgentOut = Depends(current_agent)):
        try:
            return await app.state.agents.set_name(agent.id, payload.name)
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.delete("/agents/self", status_code=204)
    async def deregister(agent: AgentOut = Depends(current_agent)):
        await app.state.agents.deregister(agent.id)
        return None
```

Wrap the SSE endpoint to mark presence (online on connect, offline on disconnect). Replace the `/events` route body with:
```python
    @app.get("/events")
    async def events(request: Request, last_event_id: int | None = None,
                     agent: AgentOut = Depends(current_agent)):
        hdr = request.headers.get("last-event-id")
        start = int(hdr) if hdr else last_event_id
        bus: EventBus = app.state.bus
        agents = app.state.agents
        await agents.set_status(agent.id, "online")

        async def gen():
            try:
                async for ev in bus.stream(agent.id, start):
                    yield {"id": str(ev.id), "event": ev.type,
                           "data": json.dumps({**ev.payload, "_id": ev.id})}
            finally:
                await agents.set_status(agent.id, "offline")

        return EventSourceResponse(gen())
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_api.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add postbox/api.py tests/test_api.py
git commit -m "v2: API — register wakeup, PATCH/DELETE /agents/self, SSE presence"
```

---

## Task 5: TmuxWakeup strategy

**Files:** Modify `postbox/listener/wakeups.py`; Test `tests/test_listener.py`

- [ ] **Step 1: Write failing tests in `tests/test_listener.py`** (append)

```python
async def test_tmux_wakeup_sends_literal_then_enter():
    from postbox.listener.wakeups import TmuxWakeup
    cmds = []
    async def fake_run(cmd): cmds.append(cmd)
    w = TmuxWakeup(pane="%7", runner=fake_run)
    await w.wake({"from": "alice", "subject": "review", "message_id": "m1"})
    # first command sends the literal text to the pane, second sends Enter
    assert cmds[0][:4] == ["tmux", "send-keys", "-l", "-t"] and cmds[0][4] == "%7"
    assert "alice" in cmds[0][5]
    assert cmds[1] == ["tmux", "send-keys", "-t", "%7", "Enter"]


def test_build_wakeup_tmux():
    from postbox.listener.wakeups import build_wakeup
    w = build_wakeup("tmux", target="%2")
    assert w.__class__.__name__ == "TmuxWakeup" and w.pane == "%2"
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/pytest tests/test_listener.py -v`
Expected: FAIL — `TmuxWakeup` undefined; `build_wakeup` doesn't know `tmux`.

- [ ] **Step 3: Implement in `postbox/listener/wakeups.py`**

Add the class (reuse the existing `_notification_text` and `_default_runner`):
```python
class TmuxWakeup:
    """Inject a notification line into the agent's tmux pane (idle interrupt)."""

    def __init__(self, pane: str, runner=_default_runner):
        self.pane = pane
        self._run = runner

    async def wake(self, event: dict) -> None:
        text = _notification_text(event)
        # -l sends the text literally (no key interpretation); Enter submits it.
        await self._run(["tmux", "send-keys", "-l", "-t", self.pane, text])
        await self._run(["tmux", "send-keys", "-t", self.pane, "Enter"])
```

Extend `build_wakeup` to accept a `target` and handle `tmux`:
```python
def build_wakeup(kind: str, repo: str = "owner/repo", target: str | None = None):
    if kind == "stub":
        return StubWakeup()
    if kind == "copilot_cli":
        return CopilotCliWakeup()
    if kind == "copilot_app":
        return CopilotAppWakeup(repo=repo)
    if kind == "tmux":
        if not target:
            raise ValueError("tmux wakeup requires a target pane")
        return TmuxWakeup(pane=target)
    if kind == "os_notify":
        return OsNotifyWakeup()
    raise ValueError(f"unknown wakeup strategy: {kind}")
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_listener.py -v`
Expected: all pass.

- [ ] **Step 5: Real-tmux integration test in `tests/test_listener.py`** (append; skipped if tmux absent)

```python
import shutil
import asyncio


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
async def test_tmux_wakeup_real_pane_receives_text(tmp_path):
    from postbox.listener.wakeups import TmuxWakeup
    session = "postbox_test_pane"
    outfile = tmp_path / "out.txt"
    # a pane that writes whatever it reads on stdin into outfile
    await (await asyncio.create_subprocess_exec(
        "tmux", "new-session", "-d", "-s", session, f"cat > {outfile}")).wait()
    try:
        await asyncio.sleep(0.3)
        proc = await asyncio.create_subprocess_exec(
            "tmux", "list-panes", "-t", session, "-F", "#{pane_id}",
            stdout=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        pane = out.decode().split()[0]
        await TmuxWakeup(pane=pane).wake(
            {"from": "alice", "subject": "hi", "message_id": "m1"})
        await asyncio.sleep(0.3)
        await (await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", pane, "C-d")).wait()   # close cat -> flush
        await asyncio.sleep(0.2)
        assert "alice" in outfile.read_text()
    finally:
        await (await asyncio.create_subprocess_exec(
            "tmux", "kill-session", "-t", session)).wait()
```

- [ ] **Step 6: Run to verify pass**

Run: `.venv/bin/pytest tests/test_listener.py -v`
Expected: all pass (the real-tmux test runs here since tmux is installed).

- [ ] **Step 7: Commit**

```bash
git add postbox/listener/wakeups.py tests/test_listener.py
git commit -m "v2: TmuxWakeup strategy (send-keys -l + Enter) with real-tmux test"
```

---

## Task 6: MCP server v2 — auto-register + background wakeup + deregister

**Files:** Modify `postbox/mcp_server.py`; Test `tests/test_mcp.py`

This is the integration centerpiece. `MailTools` (from v1) is reused for the REST calls. We add a `Session` that owns the lifecycle, and rebuild `build_server` with a `lifespan` + `instructions` + `set_name` tool.

- [ ] **Step 1: Write failing tests in `tests/test_mcp.py`** (append)

```python
import asyncio
import uvicorn
from httpx import AsyncClient
from postbox.api import create_app
from postbox.mcp_server import Session


async def test_session_autoregisters_with_pane_and_wakes(tmp_path):
    """Session registers itself (capturing the pane), and its wakeup loop pokes
    the pane when mail arrives — verified with an injected fake tmux runner.

    IMPORTANT: this drives a live SSE stream, so it MUST run against a real
    uvicorn socket — httpx ASGITransport buffers responses and cannot stream
    SSE (it would hang). Same reason test_sse.py uses a real server.
    """
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
            alice = (await c.post("/agents", json={"name": "alice"})).json()
            ah = {"Authorization": f"Bearer {alice['token']}"}

            pokes = []
            async def fake_run(cmd): pokes.append(cmd)

            # bob's session auto-registers with pane %42 and a tmux wakeup using fake_run
            bob = Session(client=c, pane="%42", desired_name="bob", runner=fake_run)
            await bob.start()
            try:
                assert bob.token                        # got a token
                assert await bob.tools.list_agents()    # directory reachable
                await asyncio.sleep(0.2)                # let the SSE loop subscribe
                await c.post("/messages", headers=ah, json={"to": "bob", "body": "ping"})
                await asyncio.sleep(0.5)                # let the wakeup fire
                assert any("%42" in cmd for cmd in pokes)         # poked bob's pane
                assert any("alice" in str(cmd) for cmd in pokes)  # names the sender
            finally:
                await bob.stop()
            online = (await c.get("/agents")).json()    # bob offline after stop
            assert all(a["address"] != "bob" for a in online)
    finally:
        server.should_exit = True
        await task
```

> **Note for implementer:** the `Session` constructor takes an httpx `client` so the test can point it at the real uvicorn socket; in production `build_server` creates `httpx.AsyncClient(base_url=COURIER_URL)`. The SSE loop uses `httpx_sse.aconnect_sse` over that client — which is why a real socket (not ASGITransport) is required here.

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/pytest tests/test_mcp.py -v`
Expected: FAIL — `Session` undefined.

- [ ] **Step 3: Implement `Session` and v2 `build_server` in `postbox/mcp_server.py`**

Keep the existing `MailTools` class. Add imports at top:
```python
import asyncio
import contextlib
from contextlib import asynccontextmanager

from httpx_sse import aconnect_sse

from postbox.listener.wakeups import build_wakeup
```

Add the `Session` class:
```python
INSTRUCTIONS = (
    "You have a Postbox mailbox for talking to other agents. "
    "When you see a line starting with '📬 New mail', immediately call check_inbox, "
    "then read_message and act on or reply to it. If unsure whether you have mail, "
    "call check_inbox at the start of your turn. Use set_name to pick your display name."
)


class Session:
    """Owns one agent's session: auto-register, background SSE wakeup, deregister."""

    def __init__(self, client, pane: str | None, desired_name: str | None,
                 runner=None):
        self.client = client
        self.pane = pane
        self.desired_name = desired_name
        self._runner = runner                  # injected tmux runner for tests
        self.token: str | None = None
        self.tools: MailTools | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        wakeup = {"kind": "tmux", "target": self.pane} if self.pane else {"kind": "none"}
        body = {"wakeup": wakeup}
        if self.desired_name:
            body["name"] = self.desired_name
        r = await self.client.post("/agents", json=body)
        r.raise_for_status()
        self.token = r.json()["token"]
        self.tools = MailTools(self.client, self.token)
        self._task = asyncio.create_task(self._wakeup_loop())

    def _build_wakeup(self):
        if not self.pane:
            return build_wakeup("stub")
        from postbox.listener.wakeups import TmuxWakeup
        if self._runner:
            return TmuxWakeup(pane=self.pane, runner=self._runner)
        return TmuxWakeup(pane=self.pane)

    async def _wakeup_loop(self) -> None:
        import json as _json
        waker = self._build_wakeup()
        # Track the last seen event id so a reconnect resumes from there rather
        # than replaying (and re-poking) the whole history. A fresh session
        # starts at 0 with an empty inbox, so first connect is clean too.
        last_id = "0"
        while True:
            try:
                headers = {"Authorization": f"Bearer {self.token}",
                           "Last-Event-ID": last_id}
                async with aconnect_sse(self.client, "GET", "/events",
                                        headers=headers) as es:
                    async for sse in es.aiter_sse():
                        last_id = sse.id or last_id
                        if sse.event == "message.received":
                            await waker.wake(_json.loads(sse.data))
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self.token:
            with contextlib.suppress(Exception):
                await self.client.delete(
                    "/agents/self", headers={"Authorization": f"Bearer {self.token}"})
```

Add `reply`/`set_name` support to `MailTools` (set_name calls PATCH):
```python
    async def set_name(self, name: str) -> dict:
        r = await self.client.patch("/agents/self", headers=self.headers,
                                    json={"name": name})
        r.raise_for_status()
        return r.json()
```

Rewrite `build_server` to use a real session + lifespan + instructions:
```python
def build_server():
    import httpx
    from mcp.server.fastmcp import FastMCP

    url = os.environ.get("COURIER_URL", "http://127.0.0.1:8765")
    pane = os.environ.get("TMUX_PANE")          # inherited inside a tmux pane
    name = os.environ.get("COURIER_NAME")       # optional desired name
    client = httpx.AsyncClient(base_url=url)
    session = Session(client, pane=pane, desired_name=name)

    @asynccontextmanager
    async def lifespan(_server):
        await session.start()
        try:
            yield {"session": session}
        finally:
            await session.stop()
            await client.aclose()

    mcp = FastMCP("postbox-mail", instructions=INSTRUCTIONS, lifespan=lifespan)

    @mcp.tool()
    async def list_agents() -> list[dict]:
        """List the agents currently online that you can message."""
        return await session.tools.list_agents()

    @mcp.tool()
    async def send_message(to: str, body: str, subject: str = "",
                           in_reply_to: str = "") -> dict:
        """Send a message to another agent by name."""
        return await session.tools.send_message(
            to=to, body=body, subject=subject or None, in_reply_to=in_reply_to or None)

    @mcp.tool()
    async def check_inbox(unread: bool = True) -> list[dict]:
        """List messages in your inbox (unread=True shows only unread)."""
        return await session.tools.check_inbox(unread=unread)

    @mcp.tool()
    async def read_message(message_id: str) -> dict:
        """Read a message by id (marks it read)."""
        return await session.tools.read_message(message_id)

    @mcp.tool()
    async def reply(message_id: str, body: str) -> dict:
        """Reply to a message, keeping it in the same thread."""
        return await session.tools.reply(message_id, body)

    @mcp.tool()
    async def set_name(name: str) -> dict:
        """Set your display name so other agents can address you by it."""
        return await session.tools.set_name(name)

    return mcp


if __name__ == "__main__":
    build_server().run()
```

> **Implementer caution:** FastMCP `lifespan`/`instructions` are confirmed present (verified). Run `.venv/bin/python -c "import postbox.mcp_server"` after editing. If the `lifespan` yield-shape differs in this SDK version, STOP and report the exact error rather than guessing (this is exactly how the v1 FK/SSE bugs were caught). Do not change the REST layer to work around it.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_mcp.py -v`
Expected: all pass (existing 3 + new 1). The new test uses a real uvicorn socket, so the SSE wakeup stream works end to end.

- [ ] **Step 5: Commit**

```bash
git add postbox/mcp_server.py tests/test_mcp.py
git commit -m "v2: MCP server self-registers, runs background tmux wakeup loop, deregisters"
```

---

## Task 7: Token-less config + tmux workflow docs

**Files:** Modify `README.md`, `CLAUDE.md`

- [ ] **Step 1: Replace the MCP/listener sections of `README.md`** with the v2 token-less, tmux-native flow:

````markdown
## Wire up MCP (one shared, token-LESS config for every agent)
`~/.copilot/mcp-config.json` — identical for all instances; **no token**:
```json
{ "mcpServers": { "postbox": {
  "type": "local",
  "command": "/Users/adachary/workspace/personal/messaging/.venv/bin/python",
  "args": ["-m", "postbox.mcp_server"],
  "env": { "COURIER_URL": "http://127.0.0.1:8765" }
}}}
```
Each Copilot instance's MCP server auto-registers its own identity on startup and
captures its `$TMUX_PANE` for real-time wakeups. Run Copilot **inside tmux** so it can be poked.

## Two agents talking, real-time (run each inside tmux)
```bash
tmux new -s a 'copilot'      # tab/pane A
tmux new -s b 'copilot'      # tab/pane B
```
In A: "set your postbox name to alice, then send a message to bob: 'review PR #42?'"
In B (idle): its pane is poked automatically — "📬 New mail from alice …" — and it
reads + replies with no prompting from you.
````

- [ ] **Step 2: Update `CLAUDE.md`** Status line to note v2 (session identity + tmux wakeup) and add `scripts/v2_tmux_e2e.py` to the index.

- [ ] **Step 3: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "v2: token-less shared config + tmux two-agent workflow docs"
```

---

## Task 8: Real-tmux end-to-end proof + final review

**Files:** Create `scripts/v2_tmux_e2e.py`

- [ ] **Step 1: Write `scripts/v2_tmux_e2e.py`** — runs the REAL server, registers a sender, starts a `Session` whose wakeup pokes a REAL tmux pane, sends mail, and asserts (via `tmux capture-pane`/stdin file) the pane received the poke.

```python
"""Real-tmux end-to-end proof of v2: a Session's wakeup loop pokes a live tmux
pane when mail arrives. Not a unit test; run manually (needs tmux)."""
import asyncio
import os
import tempfile

import httpx
import uvicorn

from postbox.api import create_app
from postbox.mcp_server import Session

OK = "\033[92mPASS\033[0m"


async def main():
    app = create_app(tempfile.mkdtemp())
    cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="on")
    server = uvicorn.Server(cfg)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    sess_name = "postbox_v2_e2e"
    outfile = os.path.join(tempfile.mkdtemp(), "pane.txt")
    await (await asyncio.create_subprocess_exec(
        "tmux", "new-session", "-d", "-s", sess_name, f"cat > {outfile}")).wait()
    await asyncio.sleep(0.3)
    proc = await asyncio.create_subprocess_exec(
        "tmux", "list-panes", "-t", sess_name, "-F", "#{pane_id}",
        stdout=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    pane = out.decode().split()[0]
    print(f"bob's tmux pane: {pane}")

    async with httpx.AsyncClient(base_url=base) as c:
        alice = (await c.post("/agents", json={"name": "alice"})).json()
        bob = Session(c, pane=pane, desired_name="bob")     # real TmuxWakeup
        await bob.start()
        await asyncio.sleep(0.3)
        await c.post("/messages", headers={"Authorization": f"Bearer {alice['token']}"},
                     json={"to": "bob", "body": "review PR #42?", "subject": "review"})
        await asyncio.sleep(0.6)
        await (await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", pane, "C-d")).wait()
        await asyncio.sleep(0.2)
        received = open(outfile).read()
        print(f"  [{OK if 'alice' in received else 'FAIL'}] bob's pane was poked: {received.strip()!r}")
        assert "alice" in received
        await bob.stop()

    await (await asyncio.create_subprocess_exec(
        "tmux", "kill-session", "-t", sess_name)).wait()
    server.should_exit = True
    await task
    print("\n\033[92mV2 REAL-TMUX E2E PASSED\033[0m — idle pane poked in real time on new mail.\n")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run the full suite + the e2e**

Run: `.venv/bin/pytest -q` (expect all pass) and `.venv/bin/python scripts/v2_tmux_e2e.py` (expect `V2 REAL-TMUX E2E PASSED`).

- [ ] **Step 3: Commit**

```bash
git add scripts/v2_tmux_e2e.py
git commit -m "v2: real-tmux end-to-end proof (idle pane poked on new mail)"
```

- [ ] **Step 4: Final whole-branch review** (dispatch a reviewer over the v2 diff for cross-cutting correctness: presence lifecycle, no poke-loops, token-less config, lifespan correctness).

---

## Deferred from spec (roadmap, not in this slice)
- **Burst debounce** (spec §5: coalesce many arrivals into one poke) — v2 pokes once per `message.received`; events go only to recipients so there is no echo loop. Coalescing is a later refinement.
- Heartbeat/TTL reaping of stale sessions; stable cross-session handles; central-relay option; non-tmux surfaces.

## Done criteria (v2)
- `.venv/bin/pytest -q` green; `scripts/v2_tmux_e2e.py` prints PASS.
- One token-less shared config; each MCP server self-registers a session identity capturing `$TMUX_PANE`.
- `set_name` lets the agent pick its handle; peers address by name; directory shows online agents.
- A message to an **idle** agent pokes its tmux pane in real time; the durable inbox + auto-check instruction remain the backstop.
