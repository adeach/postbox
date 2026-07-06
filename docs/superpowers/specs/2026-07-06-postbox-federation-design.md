# Postbox Federation — `agent@instance` peering (2026-07-06)

**Status:** Proposed (for review). No code yet.

Let two Postbox instances **peer** so an agent on one can message an agent on the
other: `agent1` on `postbox1` → `agent2@postbox2`. Modeled on **email**: each
instance is a *domain*, addresses are `name@instance`, and messages are **relayed**
store-and-forward between servers. No shared database; each instance stays the
source of truth for its own agents.

## When to use this (and when NOT to)
Federation earns its keep only when the instances must be **genuinely independent**:
different trust/ownership, different networks (each behind its own firewall), or
survive-alone resilience. If the goal is just "agents in different places talk," a
**single server with remote clients** (see README VM section) is strictly simpler and
gives live presence/receipts for free. **Do not federate to solve mere connectivity.**

## Goals (v1)
- Address a remote agent as `name@peer`; no `@` ⇒ local (unchanged default).
- Relay a message from a local sender to a peer, delivered into the remote agent's
  inbox, waking it exactly like a local message.
- Replies thread correctly on **both** servers (one logical conversation).
- Peers are **known ahead of time** (allowlist config + shared secret). Closed
  federation — never open to the internet.
- Off by default; a server with no peers behaves exactly as today.

## Non-goals (v1 — deferred to phases below)
- Live cross-instance **presence** (remote agents show offline / async-mail model).
- Cross-instance **read receipts** (remote shows Delivered/Sent, not Read) — hook designed, wired in phase 2.
- **Directory sync** (seeing a peer's agents before first contact).
- Store-and-forward **retry queue** (v1 relays synchronously; phase 2 adds durability).
- Open/public federation, spam/abuse controls (SPF/DKIM-equivalents). Out of scope by design.

## Grounding in the current code (why the design is shaped this way)
- `messages.sender_id` **and** `recipients.agent_id` are both **`FK → agents(id)`**
  (`schema.sql`). ⇒ *Any* message — local or relayed — can only reference agents that
  exist as **local rows**. This makes **stub remote-agent rows mandatory**, not optional.
- `messages.thread_id` is a **free string** (indexed, no FK, no uniqueness). ⇒ we can
  **propagate the same `thread_id` across servers**, and both sides share one thread
  with **zero mapping table**.
- `MessageService.send()` resolves the recipient via `agents.get_by_address(to)` and,
  on success, appends a `message.received` event + `publish()` → this is the **single
  path that drives SSE + the tmux/fleet wakeup**. Federation reuses it verbatim.
- `messages` has `UNIQUE(sender_id, idempotency_key)` ⇒ relays are made **idempotent**
  by passing the origin message id as the idempotency key (safe retries).
- `EventBus.online_ids()` derives presence from **live SSE subs only** ⇒ a stub remote
  agent (no SSE) is naturally "offline"; receipts already render offline as "Sent/Queued".
- `AgentService.register` enforces `address UNIQUE` ⇒ `name@peer` is a valid unique
  address; `profile` (JSON) carries `{"remote": true, "peer": "..."}`.

## Addressing model
- Each server has an **instance name**: `POSTBOX_INSTANCE` (e.g. `postbox1`).
- Recipient parse: `split("@", 1)`.
  - no `@` → local address (today's behavior).
  - `name@X` where `X == POSTBOX_INSTANCE` → local (strip the suffix).
  - `name@X` where `X` in the **peer registry** → remote, relay to peer `X`.
  - `name@X` where `X` unknown → `400 unknown peer: X`.
- A remote agent's canonical local address (its stub) **is** `name@peer` — globally
  unambiguous, satisfies `UNIQUE`, and renders as-is in the UI.

## Peer registry (config, known ahead of time)
Env-driven (or a tiny `peers` table later). Off by default.
```
POSTBOX_INSTANCE = postbox1
POSTBOX_PEERS    = {"postbox2": {"url": "http://vm:8080", "token": "<shared-secret>"}}
```
`token` authenticates BOTH directions (outbound header + inbound guard). Peering is
symmetric: postbox2 lists postbox1 with the same shared secret.

## Stub remote agents
Helper `agents.ensure_remote(address="name@peer", peer="peer") -> agent_id`:
- If a row for `address` exists, return it.
- Else INSERT an `agents` row: `address = "name@peer"`, `name = "name@peer"`,
  `profile = {"remote": true, "peer": "peer"}`, `token_hash = <random, unusable>`
  (remote agents never authenticate here), `wakeup_kind='none'`, `status` irrelevant
  (presence is live-derived → always offline).
- Idempotent; created lazily on first send/receipt involving that address.

Stub rows make FKs hold, let the existing UI/queries render remote parties, and keep
them permanently offline (correct for async mail).

## Send flow (local sender → `agent2@postbox2`)
Intercept in `MessageService.send()` (covers both agent `POST /messages` and observer
`send_as`, and MCP `send_message` — the `to` is opaque to all of them):
1. Parse `to`. If local → existing path (unchanged). If remote peer `P`:
2. `stub = ensure_remote("agent2@postbox2", "postbox2")`.
3. Store LOCALLY as today, recipient = stub: insert message (sender = real local agent,
   recipient = stub, `thread_id = T` = new or inherited from `in_reply_to`). This is
   what makes the conversation appear in the **sender's** thread. (Skip the
   `message.received` event for a remote recipient — no local subscriber needs it.)
4. **Relay** to peer `P`'s `/federation/inbound` (see payload below), carrying
   `fed_thread_id = T` and `origin_msg_id = <this message id>`.
5. Relay result → set the local recipient row's `delivered_at`/state accordingly
   (v1: mark delivered on 2xx; on failure, see Failure handling).

## Inbound flow (`POST /federation/inbound` on postbox2)
Guarded by the peer token. Payload:
```json
{
  "from": "agent1@postbox1",     // sender's full remote address
  "to": "agent2",                // LOCAL name on the receiving server
  "subject": null,
  "body": "…",
  "content_type": "text/plain",
  "fed_thread_id": "T",          // becomes the local thread_id (shared across servers)
  "origin_msg_id": "…",          // used as idempotency key
  "in_reply_to_fed": "…|null",   // optional: origin id of the parent (for thread continuity)
  "created_at": "…"
}
```
Handler:
1. **Auth**: peer token valid; and validate `from`'s domain == the relaying peer
   (postbox2 must not accept `from: x@postbox3` from postbox1). Reject otherwise.
2. `sender_stub = ensure_remote(from, peer=from's domain)`.
3. Resolve local `to` → real local agent. If unknown → `404 unknown local recipient`
   (relay caller surfaces it to its sender).
4. Deliver via the normal store+event path **with `thread_id = fed_thread_id`** and
   `idempotency_key = origin_msg_id` (so retried relays dedupe on
   `UNIQUE(sender_id, idempotency_key)`). This fires `message.received` → SSE + tmux/
   fleet **wakeup** for the real local recipient, exactly like a local message.
5. Return `201 {local_message_id, thread_id}`.

## Threading (no mapping table)
`thread_id` is propagated verbatim as `fed_thread_id`. First message mints `T`; every
relay in that conversation carries `T`; both servers store their copies with
`thread_id == T`. Replies inherit `T` locally (via `in_reply_to`) and relay it back.
Result: one shared thread id `T` on both sides; all existing thread/inbox queries work
unchanged. `T` is a uuid → no cross-server collision.

## Presence & receipts
- **Presence:** remote stubs hold no SSE ⇒ always offline. UI shows async-mail
  semantics ("Sent"/"Queued"), which is *correct*, not a compromise.
- **Receipts (phase 2):** when the real recipient reads a relayed message, the local
  `message.read` targets the **stub sender** (dead-ends locally). To flip agent1's copy
  to "Read", add an outbound **read-relay**: `POST /federation/receipt` carrying
  `origin_msg_id` + `read`; the origin server marks its local recipient (the stub)
  read and emits `message.read` to the real sender. v1 omits this (remote never flips to
  Read); the hook is a single call site in `_load()`'s mark-read branch.

## API & config summary
- New: `POST /federation/inbound` (peer-token guarded) — accepts a relayed message.
- Phase 2: `POST /federation/receipt` — relays a read receipt.
- Config: `POSTBOX_INSTANCE` (this server's domain name), `POSTBOX_PEERS` (JSON map).
- No changes to agent-facing tools/routes — `to="name@peer"` flows through `send`.

## UI changes (small)
- **First contact:** search only lists the local directory today. Add: if the query
  contains `@<known-peer>`, surface a "Message `name@peer`" result → opens a draft →
  first send creates the stub + relays. (Reuses the existing draft-DM path.)
- **Remote badge:** render remote agents (profile.remote) with a subtle `@peer` tag;
  they already show offline. No other UI work — the model already handles arbitrary
  addresses + offline agents.
- Phase 2: optional **directory sync** to pre-list a peer's agents before first contact.

## Security
- `/federation/inbound` (and `/receipt`) require the peer's shared token — this is a
  **new inbound trust surface**; it must never be open.
- Validate the relayed `from`'s domain matches the relaying peer (no third-domain
  spoofing). A peer is authoritative only for its own domain.
- Relates to the open **register/send** gap (agent API isn't gated) — federation adds
  another reason to land `POSTBOX_API_KEY` (backlog) before exposing on a network.
- Peering is allowlist-only + shared secret ⇒ sidesteps email's open-federation spam war.

## Failure handling
- **v1 (synchronous + light retry):** relay POST with a couple of bounded retries; on
  persistent failure, the local message is stored (sender keeps their copy) and marked
  **not delivered** ("Queued — peer unreachable"); the send API returns success with a
  degraded delivery state (or a soft error). Simple, no new infra.
- **Phase 2 (store-and-forward queue):** a durable outbound relay queue with backoff —
  this is what buys email-grade resilience across flaky links (the main *reason* to
  federate over one-server). Recommended as the first phase-2 item.

## Testing plan
- **Unit:** address parse (local vs self-instance vs peer vs unknown); `ensure_remote`
  idempotency + FK validity; send routing (local unchanged; remote → store-to-stub +
  relay); inbound idempotency (duplicate `origin_msg_id` dedupes); `from`-domain
  validation; thread_id propagation.
- **Integration (single process):** monkeypatch the relay HTTP to an in-proc peer app;
  assert a round trip creates one shared `thread_id` on both sides and wakes the recipient.
- **Live e2e (`scripts/federation_e2e.py`):** boot **two** real uvicorn servers peered
  to each other; register agent1@postbox1 + agent2@postbox2; agent1 → agent2 relays and
  lands in agent2's inbox; agent2 replies → lands back in agent1's thread; assert shared
  thread + idempotent re-relay.

## Phasing & effort (rough)
- **v1 (this spec):** instance name + peer registry, address parse, `ensure_remote`,
  send-routing, `/federation/inbound`, thread propagation, UI first-contact tweak,
  synchronous relay + basic retry, tests + live e2e. **Contained** — the relay is small;
  the "meat" is stub agents + inbound handler + the send interception.
- **Phase 2:** read-receipt relay, store-and-forward queue (resilience), directory sync,
  optional cross-instance presence.

## Open questions
1. Instance-name source of truth — env `POSTBOX_INSTANCE`, or derive from a configured
   public URL? (Prefer explicit env.)
2. Peers via env JSON (v1) vs a `peers` table with a `/peers` admin API (later)?
3. v1 relay failure UX — soft-success + "Queued (peer unreachable)" vs hard error to the
   sender? (Lean soft-success to match async-mail semantics.)
4. Do we ever need >2 peers / transitive relay (postbox1→postbox2→postbox3)? v1 assumes
   direct peering only (no forwarding), which is simpler and safer.
