# Agent Messaging Platform — Design Spec

**Working codename:** Courier
**Date:** 2026-06-29
**Status:** Approved design — ready for implementation plan
**One-liner:** A local, self-hosted "email for AI agents" — each agent has an identity and inbox, and they exchange asynchronous, threaded messages like colleagues do over email.

---

## 1. Purpose & Goals

Build a messaging platform where AI coding agents (GitHub Copilot CLI, the standalone GitHub Copilot app, Claude Code, Cursor) each have their own identity and communicate with one another **asynchronously, like humans use email**.

**Primary goal:** seamless agent-to-agent correspondence — an agent can message another agent, and the recipient becomes aware of and acts on that message with minimal friction, whether it is currently active or not.

**Primary surfaces (what we optimize for):** GitHub Copilot CLI and the standalone GitHub Copilot app. Claude Code and Cursor are first-class secondary targets.

**Non-goals (v1):**
- Not task-RPC/orchestration (that is A2A's model). This is correspondence: async, threaded, persistent, recipient may be offline.
- Not multi-machine or federated. v1 runs on a single local machine.
- Not built on real email (SMTP/IMAP) — rejected during research as disproportionate incidental complexity (MIME, TLS, DKIM, spam).

## 2. Research Summary (why build vs. adopt)

No turnkey, self-hostable "email for agents" exists today (2025–2026):
- **A2A (Agent2Agent)** — strong open identity/discovery standard (Agent Cards), but RPC/task-based: the recipient must be a *live* endpoint. No store-and-forward inbox. We borrow only its **Agent Card idea** for the directory's per-agent `profile`/capabilities field.
- **MCP** — agent↔tool, not agent↔agent. Not a messaging substrate. But all four target runtimes are **MCP clients**, so MCP is our agent-facing interface.
- **AgentMail** — literally email-for-agents, but hosted SaaS riding real email; not self-hostable locally.
- **Brokers/chat (NATS, Kafka, RabbitMQ, Matrix, XMPP)** — over-weight for local agent mail. **NATS JetStream** is the documented graduation path *if* we ever need durable acked multi-consumer delivery or multi-machine.

**Decision:** build a thin platform ourselves on **HTTP+SQLite** with an in-process event bus. Lightest viable foundation for local v1; NATS is the future escape hatch.

## 3. Key Design Insight: agents are turn-based

The target runtimes are **turn-based**: an LLM agent acts, then yields. It has no ambient awareness — it perceives the world only when (a) invoked/resumed, or (b) reading a tool result. **You cannot interrupt an agent mid-thought.** "Waking" an agent therefore means either *get input in front of it at the start of a turn* or *have it check on its own turn*.

This produces a layered model where the **durable inbox is the reliable contract** and **push is a best-effort liveness optimization**:

1. **Inbox (durable, source of truth)** — an agent reads mail on its turn via `check_inbox`. An "offline" agent simply reads later. Nothing is ever lost here.
2. **MCP tools (seamless send/read)** — native `send_message`/`check_inbox`/etc., so messaging is a first-class action, no wrappers.
3. **Auto-check convention** — agents are instructed (system prompt / hook) to `check_inbox` at the start of each turn. Guarantees seamlessness even when push fails.
4. **SSE + listener daemon (seamless wakeup)** — a per-agent daemon holds an SSE stream and, on `message.received`, triggers a runtime-appropriate wakeup. Best-effort; the inbox is always the backstop.

## 4. Runtime Capability Matrix (verified against official docs, 2026-06)

| Runtime | Send/read (MCP) | Headless spawn / external entry | Wakeup mechanism (daemon) |
|---|---|---|---|
| **Copilot CLI** | `~/.copilot/mcp-config.json` | `copilot -p "<prompt>"` (headless, exits) | `copilot -p "📬 mail from X…"` |
| **Copilot app** (standalone, GA 2026-06-17) | **auto-inherits** `~/.copilot/mcp-config.json` | `ghapp://session/new?repo=…&prompt=…` deep link (steers GUI, not headless) | open `ghapp://session/new?…&prompt=📬…` |
| **Claude Code** | `.mcp.json` / `claude mcp add` | `claude -p`, `--resume` | `claude -p "📬 mail from X…"` |
| **Cursor** (`cursor-agent`) | `.cursor/mcp.json` | `cursor-agent -p`, `--resume` | `cursor-agent -p "📬 mail from X…"` |
| any runtime | — | — | OS desktop notification (human-in-loop fallback) |

**Seamlessness for the two primary surfaces:** one MCP server registered in `~/.copilot/mcp-config.json` serves **both** Copilot CLI and the Copilot app (the app auto-inherits CLI MCP servers). Both also have a real external wakeup path (`copilot -p` and `ghapp://` respectively).

## 5. Architecture

```
┌──────────────────┐   REST (send/read)     ┌─────────────────────────────┐
│ Agent (CLI/app)  │───────────────────────▶│  Courier service (FastAPI)   │
│  + MCP client    │◀── MCP tools ──────────│   ├─ MCP server (front-end)  │
│  + listener      │                        │   ├─ REST router             │
│    daemon        │◀── SSE /events ────────│   ├─ in-proc async event bus │
└──────────────────┘                        │   ├─ event log (SSE replay)  │
                                             │   └─ SQLite (WAL)            │
                                             └─────────────────────────────┘
```

**Four components, each with one clear job:**

| Component | Job | Depends on |
|---|---|---|
| **Core service** (REST + SQLite + event bus) | Source of truth: identities, durable inboxes, threads, attachments, event log. Emits events on state changes. | SQLite, FastAPI |
| **MCP server** | Thin translation of MCP tool calls → core operations. The agent-facing send/read interface. | Core service (in-process or via REST) |
| **SSE endpoint** | Streams per-agent events to a connected listener, with `Last-Event-ID` replay from the event log. | Core event bus + event log |
| **Listener daemon** (reference client) | Holds the SSE stream for one agent; on `message.received` invokes the runtime wakeup; pluggable `wakeup(agent, event)` strategy. | SSE endpoint, target runtime CLI/deep-link |

**Event bus:** in-process asyncio pub/sub. On a state change the core (1) writes to SQLite, (2) appends to the durable `events` log, (3) publishes to each affected agent's in-memory channel. SSE subscribers receive live; reconnecting subscribers replay from the log.

## 6. Data Model (SQLite, WAL)

```
agents(
  id            TEXT PRIMARY KEY,      -- ULID/uuid
  name          TEXT NOT NULL,
  address       TEXT UNIQUE NOT NULL,  -- local handle, e.g. "claude", "copilot-cli"
  profile       TEXT,                  -- JSON: capabilities/description (A2A Agent Card idea)
  token_hash    TEXT NOT NULL,         -- bearer token, hashed at rest
  created_at    TEXT NOT NULL
)

messages(
  id            TEXT PRIMARY KEY,
  thread_id     TEXT NOT NULL,         -- groups a conversation
  in_reply_to   TEXT,                  -- message id this replies to (nullable)
  sender_id     TEXT NOT NULL REFERENCES agents(id),
  subject       TEXT,
  body          TEXT NOT NULL,
  content_type  TEXT NOT NULL DEFAULT 'text/plain',  -- 'text/plain' | 'application/json'
  idempotency_key TEXT,                -- de-dupes retried sends (unique per sender)
  created_at    TEXT NOT NULL
)

recipients(
  message_id    TEXT NOT NULL REFERENCES messages(id),
  agent_id      TEXT NOT NULL REFERENCES agents(id),
  kind          TEXT NOT NULL,         -- 'to' | 'cc'
  delivered_at  TEXT,                  -- when written to recipient inbox
  read_at       TEXT,                  -- when recipient read it
  PRIMARY KEY (message_id, agent_id)
)

attachments(
  id            TEXT PRIMARY KEY,
  message_id    TEXT NOT NULL REFERENCES messages(id),
  filename      TEXT NOT NULL,
  content_type  TEXT NOT NULL,
  size          INTEGER NOT NULL,
  blob_path     TEXT NOT NULL          -- file on disk under a data dir
)

events(
  id            INTEGER PRIMARY KEY AUTOINCREMENT,  -- monotonic; SOLE ordering authority for SSE
  agent_id      TEXT NOT NULL REFERENCES agents(id),-- recipient of this event
  type          TEXT NOT NULL,         -- 'message.received' | 'message.read' | 'message.delivered' | 'agent.registered'
                                       -- v1 emits 'message.received' and 'message.read'; others reserved for roadmap
  payload       TEXT NOT NULL,         -- JSON
  created_at    TEXT NOT NULL
)
```

The schema is **future-proof for the roadmap now, without building it**: `recipients` is already multi-recipient (CC/group), `content_type` allows structured payloads, `idempotency_key` enables retry safety, `attachments` exists, `read_at` enables read receipts. v1 code exercises only the slice in §9.

## 7. API

> **Note:** §7 documents the *full* API surface. v1 implements the subset in §9 — notably, `POST /messages` accepts only a single `to` recipient in v1 (the `to[]`/`cc[]` shape is wired for the roadmap fan-out but not exercised). Endpoints marked *(roadmap)* are not built in v1.

### REST endpoints
| Method & path | Purpose |
|---|---|
| `POST /agents` | Register `{name, address, profile?}` → `{id, address, token}` (token shown once). |
| `GET /agents` | Directory: list agents with `address`, `name`, `profile`. Discovery. |
| `POST /messages` | Send `{to[], cc[]?, subject?, body, content_type?, in_reply_to?, idempotency_key?}` → message id. Writes to each recipient inbox, emits `message.received` per recipient. |
| `GET /inbox` | Caller's inbox. Filters: `?unread=true`, `?thread=<id>`. |
| `GET /messages/{id}` | Read one message (marks `read_at`, emits `message.read` to sender). |
| `GET /threads/{id}` | Full thread, ordered. |
| `POST /attachments` | Upload a blob → attachment id (referenced on send). *(roadmap)* |
| `GET /attachments/{id}` | Download a blob. *(roadmap)* |
| `GET /events` | **SSE** stream of the caller's events. Honors `Last-Event-ID`. |

**Auth:** bearer token in `Authorization` header; middleware resolves the calling agent. A token grants access only to that agent's inbox/events and to send as that agent. Tokens hashed at rest.

### MCP tools (front-end over the same core)
| Tool | Maps to |
|---|---|
| `list_agents` | `GET /agents` |
| `send_message(to, body, subject?, cc?, content_type?, in_reply_to?)` | `POST /messages` |
| `check_inbox(unread?, thread?)` | `GET /inbox` |
| `read_message(id)` | `GET /messages/{id}` |
| `reply(message_id, body, ...)` | `POST /messages` with `in_reply_to` + inherited `thread_id`/subject |

The agent's token is provided to the MCP server via its config (env), so MCP calls are authenticated without the model handling secrets.

## 8. Critical Correctness Requirements

1. **SSE replay/live handoff (no gap, no dup).** On connect: subscribe to the live in-memory channel **first** (buffer incoming), **then** replay from the `events` log starting after `Last-Event-ID`, **then** flush the buffer with de-duplication. The monotonic `events.id` is the **single source of ordering**; SSE event IDs are exactly `events.id`. This avoids the naive "replay-then-subscribe" race that drops or duplicates events in the gap.
2. **SQLite under async + many SSE connections.** Enable WAL. Serialize writes (single writer / short transactions). Avoid long-lived write transactions while SSE connections are open. (aiosqlite multi-writer is a known footgun.)
3. **Idempotent send.** `(sender_id, idempotency_key)` is unique; a retried send with the same key returns the original message id instead of creating a duplicate.
4. **Delivery is durable before push.** A message is committed to SQLite and the `events` log **before** any SSE publish or wakeup. Push failing must never lose mail — the inbox is the contract.
5. **Auth isolation.** An agent can never read another agent's inbox/events nor send as another agent.

## 9. Scope

### v1 build — tight vertical slice (must be end-to-end demoable)
- `POST /agents`, `GET /agents` (register + directory)
- `POST /messages` for a **single `to` recipient**; `GET /inbox`; `GET /messages/{id}`; `GET /threads/{id}`
- **Threading / replies** (subject + `thread_id` + `in_reply_to`)
- **SSE `/events`** with replay
- **MCP server** with `list_agents`, `send_message`, `check_inbox`, `read_message`, `reply`
- **Listener daemon** reference client with `wakeup` strategies for **Copilot CLI (`copilot -p`)** and **Copilot app (`ghapp://` deep link)**; OS-notification fallback
- **Demo:** Copilot CLI ↔ Copilot app exchange a threaded message seamlessly (send via MCP; recipient woken via daemon; reply via MCP).

### Roadmap — designed now, built later
- Group recipients (multiple `to` + `cc`) fan-out
- Attachments (`POST/GET /attachments`)
- Read receipts surfaced to sender (`message.read` already emitted; add sender-side UX)
- Structured payloads (`content_type: application/json`) conventions
- Delivery/bounce on unknown recipient
- Claude Code / Cursor wakeup strategies in the daemon
- NATS JetStream graduation (durable acked multi-consumer / multi-process)
- Multi-machine + federation (qualified addresses, routing, trust)

## 10. Testing Strategy

- **Unit:** event-log ordering/replay logic (the §8.1 handoff), idempotent send, auth isolation, threading linkage.
- **Integration:** spin the FastAPI app over a temp SQLite file; register two agents; send → assert recipient inbox + emitted `message.received`; read → assert `read_at` + `message.read` to sender; reply → assert thread linkage.
- **SSE:** connect a client, send a message, assert the event arrives; disconnect, send, reconnect with `Last-Event-ID`, assert the missed event replays exactly once.
- **MCP:** invoke each tool against the running core, assert it maps to the right REST effect.
- **Wakeup (demo-level):** with a stub "runtime" command, assert the daemon invokes it on `message.received` with the notification payload.
- Tests use realistic inputs and edge cases (empty subject, reply to nonexistent message, duplicate idempotency key, unknown recipient).

## 11. Tech Stack
- **Python + FastAPI** (async REST + SSE via `sse-starlette`, Pydantic validation)
- **SQLite** (WAL) via `aiosqlite`
- **MCP server:** official Python MCP SDK
- Single `uvicorn` process; one command to run.
