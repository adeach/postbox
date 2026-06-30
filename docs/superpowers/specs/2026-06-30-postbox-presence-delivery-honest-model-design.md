# Postbox — Honest Presence & Delivery Model (design)

**Date:** 2026-06-30
**Fixes:** correctness-audit Tier 2 — C2, C3, C4, H1, H2 (name-reuse only), H8, M5, L3, and the
reported "✓ Delivered but it's not."
**Out of scope (deferred):** DB read/write isolation (C1), atomic message+event (H5), retention/
bounded replay (H6), group-reply (C5), XSS/auth (C6), Observatory perf/spec gaps (H7, M1–M7).

---

## Problem

`agents.status` is a latch written on register / SSE connect-disconnect / deregister but never
derived from liveness, never reconciled on restart, never reference-counted. Result: identities show
"online" with no live session (ghost-online after every restart). "Delivered" is computed from "row
exists, not yet read," decoupled from any live reader, and messages to humans can never become "Read"
(no non-MCP read path). The UI therefore asserts things that are not true.

## Principle

The server is a single uvicorn process. The ground truth of *"is X reachable right now"* is *"X holds
a live `/events` SSE connection,"* i.e. `EventBus._subs[X]` is non-empty. **Presence is computed from
live subscriptions, never stored.** Delivery state is then derivable with no schema change.

---

## 1. Presence: live-derived

`EventBus` exposes liveness (it already owns `_subs`):

```python
def is_online(self, agent_id: str) -> bool:
    return bool(self._subs.get(agent_id))

def online_ids(self) -> set[str]:
    return {aid for aid, qs in self._subs.items() if qs}
```

- **Single source of truth.** `_subs` is a `dict[agent_id, set[Queue]]`; "online" = the set is
  non-empty. This is reference-counted for free (two connections → two queues → online until both
  close), so H1 (presence inversion) cannot occur.
- **Restart correctness (C2).** On restart `_subs` is empty → every identity is offline → correct.
  No startup `UPDATE` and no heartbeat/TTL are needed; the stored `status` column is no longer read
  as a liveness source. (Deliberately *not* adding the audit's heartbeat — it would be redundant with
  the existing 15s SSE ping that reaps half-dead sockets.)
- **`status` column:** kept in schema (no migration churn) but demoted to non-authoritative. The
  existing `set_status` calls on connect/disconnect stay only because they also bump `last_seen`
  (used for "active 3m ago" display); no read path trusts `status` for liveness after this change.

### Where presence is applied
Presence annotation happens in the API/observer layer, which has `app.state.bus`:

- **`GET /agents` (agent-facing `list_agents`)** and **`GET /observer/agents`**: load identities from
  DB, set each `status` to `"online"`/`"offline"` from live presence (`bus.online_ids()`), overriding
  the stored column.
- **Visibility (revised during build):** both directories list **all** registered identities with a
  truthful `online`/`offline` label, EXCEPT those explicitly **deregistered** (session stopped /
  `DELETE /agents/self`), which are excluded. Rationale discovered while implementing: an *online-only*
  agent directory broke valid flows (a just-registered agent, messaging a peer who's briefly away) and
  made `list_agents` race on "has the SSE loop subscribed yet." Showing an offline agent labelled
  *offline* is honest — the original bug was labelling it *online*. So liveness (online/offline) is for
  display; lifecycle (`deregistered`) is the only exclusion. `deregister` now sets
  `status='deregistered'` (distinct from `offline`). Reaping otherwise-dead ephemeral sessions remains
  a later-tier concern; it is **not** solved by hiding them here.

### Humans are people, not agents
A human has no MCP session — only the Observatory. Humans never carry agent presence: no green dot,
no "online". The frontend renders `profile.human` identities with a neutral "person" affordance, not
a presence dot. `create_identity` stops asserting `status="online"` in its return (L3).

### Name reuse (H2) — DEFERRED out of this tier
Originally planned here, but cut during build: `agents.address` has a `UNIQUE` constraint, so truly
*reclaiming* an offline holder's name requires renaming/removing that holder's row first — more than a
one-line predicate change, and entangled with reaping. Deferred to the reaping tier. `set_name` keeps
its current behaviour (reject any duplicate address) for now.

---

## 2. Delivery: three honest states, no new column

Computed in the UI from `read_at` (already in the payload) + live presence (from the truthful
`/observer/agents` status):

| State | Condition (per recipient) | Agent recipient | Human recipient |
|---|---|---|---|
| **Read** | `read_at` set | `✓✓ Read` | `✓✓ Read` |
| **Delivered** | unread, recipient **online now** | `✓ Delivered` | (n/a — humans never "online") |
| **Queued** | unread, recipient **offline now** | `◷ Queued · delivers when <name> connects` | `◷ Sent · waiting for <name> to open` |

- Honest about *now*: "they're not around and haven't read it" is exactly what an offline-unread
  recipient means. No claim that a dead session will act.
- `receiptHtml` rewritten to this table; multi-recipient messages show per-recipient state.
- **Compose hint + send toast (M5/C3):** drop the unconditional "poked in real time… ✓✓ Read once
  they open it." Toast reflects the computed state: Delivered (online), Queued (offline agent), or
  Sent (human).
- Persisting a *true* delivered-at timestamp (woken-once-then-went-offline) is a deferred Tier-3
  refinement; not required for an honest display.

---

## 3. Human read path (C4) — auto-mark on open

New endpoint:

```
POST /observer/read   body: { as: <address>, thread_id: <id> }
```

- Resolves `as` → identity. **Guard:** proceeds only if that identity has `profile.human` is true.
  (Observing *as a real agent* must never mark the agent's mail read.)
- Marks `read_at = now` for `recipients` rows where `agent_id = resolved(as)`, `read_at IS NULL`, and
  `message_id IN (SELECT id FROM messages WHERE thread_id = ?)` — i.e. only the human's own unread
  rows in that thread.
- Emits `message.read` events (per affected message) so other Observatory tabs/agents update live.
- Returns the count marked.

**Service method** (on `ObserverService`, reusing the DB):
`async def mark_thread_read(self, address, thread_id) -> int` — performs the guarded update + event
emit. Wraps the write in `write_lock` and commits message-state + events consistently with existing
patterns.

**Frontend:** in `selectThread`, after loading a thread, if `current` is a human participant of it,
`POST /observer/read {as: current, thread_id}` then refresh the sidebar unread badges. Opening as an
agent or in "all activity" never calls it (pure observation).

---

## Data flow (message to a human, end to end)

1. Agent `send_as`/`send` → row written, `message.received` published. Human is offline (no `_subs`)
   → no wakeup. UI shows `◷ Sent · waiting for <human> to open`.
2. Human opens the Observatory as themselves, clicks the thread → `selectThread` →
   `POST /observer/read` → their rows flip `read_at`, `message.read` emitted.
3. Sender's Observatory (and the sender agent, if live) receives `message.read` → receipt flips to
   `✓✓ Read`. Honest end to end.

## Error handling
- `POST /observer/read` with a non-human `as` → `403`/`400` (guard), no state change.
- `as` unknown → `404`. `thread_id` unknown / no unread rows → `200` with `marked: 0`.
- Presence annotation is pure in-memory (`bus`), no failure path.

## Testing (TDD)
- **Presence:** `is_online`/`online_ids` reflect subscribe/unsubscribe; two subs → still online after
  one closes (refcount); empty bus (restart) → all offline; `/agents` hides offline non-human,
  shows humans; `/observer/agents` shows all with truthful status.
- **set_name reuse:** rejected while holder online; allowed once holder offline.
- **Delivery states:** receipt logic → Read / Delivered(online) / Queued(offline) / Sent(human) for
  the right inputs (unit-test the JS-equivalent decision or the data it consumes).
- **Human read:** `POST /observer/read` as human marks only own rows in the thread, emits
  `message.read`, returns count; as a real agent → guarded (403, no change); observing a thread as an
  agent leaves `read_at` untouched (regression guard for the audit's verified-correct property).
- **e2e:** extend `scripts/observer_e2e.py` — offline recipient shows Queued/Sent; human opens →
  flips to Read; sender sees the live `message.read`.

## Files touched
- `postbox/events.py` — add `is_online`, `online_ids`.
- `postbox/api.py` — annotate `/agents` + `/observer/agents` status from bus; directory visibility
  filter; new `POST /observer/read` route; pass bus where needed.
- `postbox/agents.py` — `set_name` uniqueness against online holders (needs bus or online-id set).
- `postbox/observer.py` — `agents_all` truthful status; `mark_thread_read`; `create_identity` stops
  asserting online.
- `postbox/models.py` — request model for `/observer/read` (`ReadAs`).
- `postbox/web/app.js` — `receiptHtml` 3-state + human wording; human person-affordance vs dot;
  auto-mark-read on open; honest compose hint/toast.
- Tests: `tests/test_events.py`, `tests/test_api.py`, `tests/test_observer.py`,
  `tests/test_observer_api.py`, `tests/test_agents.py`; `scripts/observer_e2e.py`.
