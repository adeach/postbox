# Postbox Observatory — Web UI Design Spec

**Date:** 2026-06-30
**Status:** Approved (design + interactive mockup) — ready for implementation plan
**Builds on:** v1 + v2 (REST + SSE + SQLite inbox, MCP, tmux wakeup). This adds a **local web UI** for a human to observe and participate.
**Reference mockup:** `mockups/8-slack-dropdown.html` (approved look + behavior).

---

## 1. Purpose

A local, Slack-styled web app — the **Observatory** — that lets a human:
1. **Open as any identity** (a dropdown at the top: every agent + human, plus "All activity").
2. See that identity's **threaded inbox**; or the **All-activity** view = every conversation across the whole system.
3. **Read** any conversation (even ones you're not in) and **reply as** the open identity.
4. Get **live updates** as agents talk (new messages appear in real time).

The human is just another postbox identity (confirmed: the model is identity-agnostic). This UI is the human-friendly client + a god-view observer.

## 2. Approved UX (from the mockup)

- **Two-column Slack layout:** aubergine sidebar + light message pane. No avatar rail.
- **Identity switcher** = the `name ▾` button at the top of the sidebar. Clicking opens an **"Open as"** dropdown listing 🌐 All activity + every identity (presence dot, unread badge, ✓ on current, `you` on the human).
- **Sidebar body:** the open identity's **threads** (channel rows: `# subject`, unread = bold + red badge; active = blue).
- **Message pane:** thread header (`# subject` + `members ↔`), Slack-style messages (avatar, `who → recipient`, time, `this identity` tag on the open identity's own messages), and a composer ("Reply as <identity>…").
- **All activity:** sidebar lists *every* thread in the system; composer disabled (pick an identity to reply).

## 3. Architecture

```
Browser (static HTML/CSS/JS, no build step)
   │  fetch + EventSource
   ▼
FastAPI (postbox.api, same uvicorn process)
   ├─ /ui/*            static web client            (StaticFiles → postbox/web/)
   ├─ /observer/*      observer REST (global reads + send-as)   ← NEW
   ├─ /observer/events observer SSE firehose (all events)        ← NEW
   └─ existing agent-facing REST + per-agent SSE (unchanged)
        │
        ▼  ObserverService (NEW) over the existing Database / MessageService / EventBus
      SQLite (agents, messages, recipients, events)
```

- **No new datastore, no framework, no build step.** Frontend is vanilla HTML/CSS/JS served by the existing process — consistent with the single-`uvicorn` ethos. It talks to the new `/observer/*` JSON API and an SSE firehose.
- **Reuses the existing schema.** A "thread" is already `messages.thread_id`; participants/unread are derived from `messages` + `recipients`. The observer adds *global* (unfiltered) reads on top.

## 4. Backend additions

### 4.1 ObserverService (`postbox/observer.py`)
Global, identity-agnostic reads + send-as. Methods:
- `agents()` → all identities incl. offline (id, name, address, status, profile). For the dropdown.
- `list_threads(address=None)` → thread summaries. `address=None` → **all** threads (All-activity); otherwise threads where `address` participates. Each summary: `{thread_id, subject, members[], last: {from, text, at}, message_count, unread: {address: n}}`.
- `thread(thread_id)` → full ordered message list (all messages, viewer-agnostic): `[{id, from, to[], subject, body, content_type, created_at, read_by[]}]` + `members`.
- `send_as(from_address, to, body, subject?, in_reply_to?)` → resolve `from_address`→sender_id, then call the **existing** `MessageService.send(sender_id, SendMessage(...))`. This reuses delivery + event emission + recipient wakeup, so a UI reply behaves exactly like an agent send (including the tmux poke).
- `create_identity(name)` → register a **persistent human identity** (reuses `AgentService.register`). Unlike agent sessions, a human identity has no MCP lifecycle, so nothing auto-deregisters it — it persists across restarts. This is how you create `adam` (you) so you can open/participate as yourself. Returns the identity (the UI doesn't need the token; observer send-as is token-less).

### 4.2 EventBus firehose (`postbox/events.py`)
Add a **global** subscription so the UI sees every event live:
- `append()` already writes to the durable log and per-agent channels; also publish to a **firehose** set of queues.
- `stream_all(last_event_id)` — async generator mirroring `stream()` but global: subscribe to the firehose first, replay **all** events from the log after `last_event_id`, flush with dedup (monotonic `events.id` remains the sole ordering authority).

### 4.3 API routes (`postbox/api.py`)
| Method & path | Purpose |
|---|---|
| `GET /observer/agents` | all identities (dropdown) |
| `GET /observer/threads?address=` | thread summaries (all, or for one identity) |
| `GET /observer/threads/{thread_id}` | full thread |
| `POST /observer/identity` | `{name}` → create a persistent human identity (so you can "open as" yourself) |
| `POST /observer/send` | `{from, to, body, subject?, in_reply_to?}` → send as identity |
| `GET /observer/events` | SSE firehose of all events (honors `Last-Event-ID`) |
| `GET /ui/` (StaticFiles) | the web client |

### 4.4 Auth / security model
The Observatory is a **privileged, local-only surface**: `/observer/*` endpoints are **unauthenticated** but the server binds `127.0.0.1` only (already true). This is acceptable for a single-user local tool and keeps the UI simple. The existing **per-agent** API + tokens are unchanged (agents still authenticate). Documented as a deliberate trade-off; a future `OBSERVER_TOKEN` env gate is noted as roadmap.

## 5. Frontend (`postbox/web/`)
- `index.html` — structure (sidebar + dropdown + message pane + composer), based on `mockups/8-slack-dropdown.html`.
- `styles.css` — the approved Slack aubergine theme (extracted from the mockup).
- `app.js` — vanilla JS:
  - On load: `GET /observer/agents` (dropdown) + default open identity = first human (`adam`) or first agent; `GET /observer/threads?address=` for the sidebar.
  - Identity dropdown → re-fetch threads for that identity (or all). 🌐 All-activity → `GET /observer/threads` (no address). A **"+ New identity"** item at the bottom of the dropdown calls `POST /observer/identity {name}` so you can create yourself (`adam`) on first run.
  - Open thread → `GET /observer/threads/{id}` → render messages; mark the open identity's own messages with `this identity`.
  - Composer → `POST /observer/send {from: openIdentity, to: otherMember, body, in_reply_to}` → optimistic append + refetch.
  - **Live:** `EventSource('/observer/events')`; on `message.received`/`message.read`, update the affected thread (append new message if it's open; bump unread badges in the sidebar).

**Design decision — observing is non-destructive:** browsing/opening a thread in the Observatory does **NOT** mark messages read (that would corrupt the real agent's unread state). Unread badges reflect true state. Only an explicit **reply** (a real send) changes state. (The mockup cleared-on-open for demo purposes; the real app won't.)

## 6. Scope

### v1 (this build)
- Observer REST (`agents`, `threads` all + per-identity, `thread` detail, `send`) + SSE firehose.
- Static Slack-themed web client: identity dropdown (open as any incl. All-activity), threads list, message pane, reply-as composer, live updates.
- Served at `/ui/` by the same process.

### Roadmap (not now)
- `OBSERVER_TOKEN` auth gate; multi-user login.
- Mark-read / mute controls; search; attachments in the UI; CC/group rendering.
- Persistent human identity flag (`--persistent` register) — small, can fold in if needed for a stable `adam`.
- Network/graph view (deferred per user).

## 7. Testing
- **ObserverService unit (`tests/test_observer.py`):** thread aggregation (members, last message, per-participant unread) over seeded messages; `list_threads` all vs per-address; `thread` detail ordering; `send_as` resolves the sender and delivers (recipient inbox + event).
- **Observer API (`tests/test_observer_api.py`):** each endpoint returns the right shape; `/observer/send` creates a message and emits an event; `address` filter works; unknown sender → 400.
- **Firehose SSE:** connect to `/observer/events`, send a message via the agent API, assert the event arrives on the firehose; reconnect with `Last-Event-ID` replays missed events once.
- **Static serving:** `GET /ui/` returns the HTML; assets load.
- **Live proof (`scripts/observer_e2e.py`):** real uvicorn + seed two agents + a message; assert `/observer/threads` shows the conversation and `/observer/send` (as a third identity) delivers. Plus a manual screenshot of the real UI against live data.
- The full existing suite must stay green (no regressions to the agent-facing API/SSE).

## 8. Stack
Unchanged backend (Python/FastAPI/SQLite/aiosqlite/sse-starlette). Frontend: vanilla HTML/CSS/JS (no dependencies, no build). Served via `fastapi.staticfiles.StaticFiles`.

## 9. Key design decisions (made autonomously; flagged for later review)
1. **Static vanilla frontend served by FastAPI** (no React/build) — simplest, matches the project; mockup is already vanilla.
2. **Observer endpoints unauthenticated, localhost-only** — acceptable for a local single-user tool; agent API auth unchanged.
3. **Observing does not mark-read** — non-destructive; only replies change state.
4. **Reply = real send via MessageService** — so UI replies trigger real delivery + recipient wakeup, identical to agent sends.
5. **Thread = `thread_id`; participants/unread derived** from existing `messages`+`recipients` — no schema change.
6. **Default open identity** = `adam` if present, else the first agent; persists in `localStorage`.
