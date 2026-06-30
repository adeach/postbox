# Postbox — Fundamental Correctness Audit (2026-06-30)

Triggered by: the "✓ Delivered but it's not" report, and the maintainer's instruction to *stop
declaring "done" and do detailed research* — there are multiple fundamental correctness bugs, not
one cosmetic slip.

Method: four parallel adversarial auditors traced real scenarios through the actual code
(`agents.py`, `api.py`, `messages.py`, `observer.py`, `events.py`, `db.py`, `web/app.js`,
`mcp_server.py`, `listener/wakeups.py`, `schema.sql`) against the v2 + Observatory specs. Findings
deduplicated below. **[VERIFIED]** = I reproduced/confirmed it directly this session, not relayed.

## The three root causes (almost everything traces to one of these)

1. **Presence is a latch, not a liveness signal.** `agents.status` is *written* in 5 places
   (register→online, SSE connect/disconnect, deregister) but *read* in essentially one
   (`directory()` `WHERE status='online'`). It is never derived from a heartbeat/TTL, never
   reconciled on restart, and never reference-counted. The v2 spec designed an "online" predicate;
   the code wired it into one query and trusts a stale column everywhere else.

2. **"Delivered" means "a row was written," fully decoupled from any live reader.** `send` never
   checks recipient liveness and never triggers a wakeup (the wakeup is a *separate* SSE subscriber
   loop). `delivered_at` is a dead column; the UI synthesizes "✓ Delivered" from "not yet read."

3. **One shared SQLite connection in deferred-isolation mode; reads bypass the write lock.** WAL's
   snapshot isolation needs ≥2 connections — there is one — so reads can observe another coroutine's
   uncommitted, half-written transaction.

---

## CRITICAL

### C1. No read/write isolation — reads see uncommitted, partial writes **[VERIFIED]**
`Database` holds one `aiosqlite` connection (`db.py:15,19`), default `isolation_level=''` (deferred).
`fetchone/fetchall` (`db.py:64-70`) never take `write_lock`, while `messages.send` holds it across two
INSERTs with `await`s between (`messages.py:52-64`). A read scheduled in that window runs on the same
connection and sees the message row with no recipient yet.
**Repro (ran this session):** mid-write, `SELECT id FROM messages` → `[('m1',)]`, recipients → `[]`,
recipient's inbox `JOIN recipients` → `[]` (message exists but is invisible to its recipient).
**Impact:** `observer.thread` returns `to=[]/read_by=[]`; an agent's `inbox` transiently omits a
just-sent message; a rolled-back write can be read. Root architectural defect — WAL is decorative.
**Fix direction:** route reads through `write_lock`, or give readers a separate connection so WAL
isolation actually applies; make write transaction boundaries explicit (`BEGIN IMMEDIATE`/`COMMIT`).

### C2. Ghost-online after restart — `status='online'` persists, in-memory subs don't **[VERIFIED]**
`EventBus._subs/_firehose` are in-memory (`events.py:29-30`), lost on restart. The offline-flip in
`_migrate` (`db.py:42-43`) runs **only the one time the `status` column is first added** — on a fresh
DB the schema already has `status DEFAULT 'online'`, so it never runs; on later restarts there is no
reconciliation. `lifespan` (`api.py:30-31`) closes the DB without resetting status.
**Impact:** after any restart (the normal way it's run), every previously-online identity shows
`online` in `/agents`, `/observer/agents`, and the directory, with zero live subscribers — the exact
"shows online but isn't" the maintainer caught.
**Fix direction:** unconditional `UPDATE agents SET status='offline'` on startup; derive "online" from
a `last_seen` TTL instead of trusting the latch.

### C3. "✓ Delivered" asserts only that a row exists — not that any reader is live or was woken
`send` (`messages.py:59-63`) writes the recipient row with `delivered_at=created` unconditionally,
never reading `recipient.status` and never firing a wakeup; the wakeup is a separate subscriber loop
(`mcp_server.py:105-125`) that only acts if that identity holds a live SSE connection. `delivered_at`
is never read; `receiptHtml` (`app.js:85-92`) shows "✓ Delivered" purely from "in `to`, not in
`read_by`."
**Impact:** send to an offline/dead/human identity → "✓ Delivered" with no live reader and no wakeup;
the message just sits. Sender believes it landed somewhere it will be acted on.
**Fix direction:** define three honest states — Queued (no live reader), Delivered (handed to a live
session / wakeup fired), Read — and drive them from real liveness + wakeup outcome.

### C4. Messages to humans can NEVER become "Read" (no non-MCP read path)
The only `read_at` writer is `messages._load` via `GET /messages/{id}` (`messages.py:99`,
`api.py:81`), which requires a bearer token = an MCP session. There is **no `/observer/read` route**.
`create_identity` registers humans `status="online"` permanently (`observer.py:99`; offline only set
at `api.py:151`, which humans never hit). `isOnline()` returns true, so the "offline" honesty note in
`receiptHtml` is suppressed.
**Impact:** the Observatory's headline use case — message a human — shows a bare "✓ Delivered" that can
*never* flip to "✓✓ Read," with the caveat hidden. "Delivered, never Read" is structurally permanent.
**Fix direction:** an `/observer/read?as=<human>` that marks read **only** when the identity is a human
participant (never on mere observation, never when opened "as" a real agent — that would corrupt the
agent's read state); and stop marking humans blanket-"online."

### C5. Group-thread replies silently misroute to the alphabetically-first member
Reply target = `t.members.find(m=>m!==current) || t.members[0]` (`app.js:163`), and members are
`sorted` (`observer.py:54,93`). `send` is structurally single-recipient (`messages.py:59-63`,
`SendMessage.to` is one string). The UI renders threads as group chats ("members ↔ a ↔ b ↔ c",
`app.js:99`).
**Impact:** in any 3+ party thread, "Reply" always goes to the alphabetically-first other person,
deterministically wrong; the other participants never receive it. UI misrepresents a chain of 1:1
messages as a group.
**Fix direction:** restrict to true 1:1 threads (and render as such), or target the last inbound
sender, or add a recipient picker; real fix is multi-recipient support (out of v1 scope).

### C6. Stored XSS → full impersonation on an unauthenticated god-view
`a.address` is interpolated into `value="${a.address}"` with **no escaping** (`app.js:132`; `esc` is
text-only and explicitly not attribute-safe, `app.js:5`). `register` does **no** sanitization of
`name`/`address` (`agents.py:14-19`) and is reachable. `/observer/*` is unauthenticated by design and
`send_as` can send as **any** identity (`api.py:114`, `observer.py:101-107`).
**Impact:** a crafted agent name breaks out of the attribute → script in the Observatory origin, which
can read every conversation (firehose) and forge messages from anyone. Even without XSS, the unauth
send-as-anyone is a hole on a shared/multi-process machine.
**Fix direction:** attribute-escape (`escAttr` for `"`/`'`) at all attribute sinks; slug-validate
`address` at registration; land the roadmapped `OBSERVER_TOKEN` gate.

---

## HIGH

### H1. Presence toggle is not reference-counted (presence inversion)
`/events` sets online on connect, offline in `finally` (`api.py:143,151`), keyed only by `agent.id`.
Two concurrent SSE connections (or a reconnect racing the old teardown, `mcp_server.py:112-125`): the
first to disconnect flips the identity offline while the second is still live.
**Fix:** per-agent connection refcount; offline only at zero (combined with the heartbeat).

### H2. Status-blind namespace — names squatted forever; dead identities addressable
`set_name` uniqueness checks `WHERE address=? AND id<>?` with **no `status='online'`** (`agents.py:35`)
— the spec says reject only against another *online* agent (spec:61). `get_by_address`/`send` have no
status predicate either. `deregister` is soft (row kept, `agents.py:49`).
**Impact:** every handle is burned on first disconnect; a relaunched agent can't reclaim its own name;
dead sessions stay addressable forever (and accumulate — no reaping anywhere).
**Fix:** add `status='online'` to the uniqueness check and define explicit semantics for messaging
offline identities; TTL-reap/hard-delete dead ephemeral identities.

### H3. Failed wakeups are invisible to the sender
`TmuxWakeup.wake` can exhaust retries and log "GAVE UP" (`wakeups.py:148`) updating **nothing**; same
for capture/subprocess failure. The sender still sees "✓ Delivered."
**Impact:** "Delivered, never Read" conflates {never poked, poke failed, no reader exists, human can't
ack} into one indistinguishable state. "Ignored me" is indistinguishable from "never reached a reader."
**Fix:** feed wakeup outcome back into delivery state (a `wakeup_failed` event the UI renders).

### H4. Idempotency TOCTOU → uncaught 500 (and possibly a poisoned shared transaction)
The idempotency precheck (`messages.py:25-31`) runs outside `write_lock`. Concurrent duplicates both
pass; the unique index `ux_messages_idem` (`schema.sql:25-26`) prevents a duplicate row but raises
`IntegrityError`, which is **not** `ValueError` — and `api.py:73` only catches `ValueError` → HTTP 500,
with a failed statement mid-`write_lock` on the *shared* connection (no rollback path).
**Fix:** drop the precheck, rely on the index, catch `IntegrityError` → rollback → re-fetch and return
the existing message. Ensure every `write_lock` block has a rollback path. (Same pattern bites
`set_name`, `agents.py:34-40`.)

### H5. Message and its event are not atomic — crash = lost wakeup
`bus.append` (`messages.py:68`) commits the event in a *separate* transaction after the message commit
(`:64`). A crash between them leaves a durable message with no `message.received` event → the listener
never wakes the recipient. (Bounded by "inbox is the source of truth," but the real-time feature is
silently skipped.)
**Fix:** write message row + event in one transaction; publish to the in-memory bus after commit.

### H6. Unbounded growth + full firehose replay from id 0 on every UI load
No `DELETE`/TTL/reap/VACUUM anywhere **[VERIFIED: repo-wide search finds none]**. Agents, messages,
recipients, events grow forever. The Observatory opens `new EventSource("/observer/events")` with **no
`Last-Event-ID`** (`app.js:184`), so `stream_all(after=0)` (`events.py:85-86`) re-reads and re-streams
the entire event log on every page load and every auto-reconnect — cost grows linearly with history.
**Fix:** retention/reaping job; persist + send last seen event id (or cap replay to a recent window).

### H7. Firehose refresh storm — read pane unusable under real traffic
`connectLive` ignores the event payload and, on *every* system-wide `message.received/read`, runs
`loadThreads()` + full `selectThread(openThread)` which ends in `scrollTop = scrollHeight`
(`app.js:185-187,119`).
**Impact:** while you read one thread, any unrelated agent traffic forces a full re-render + scroll-to-
bottom (and re-runs the N+1 thread queries) — exactly the multi-agent scenario the Observatory exists
for.
**Fix:** use the event's `thread_id` (already emitted, `messages.py:69`/`api.py:132`) to refresh only
the affected thread; preserve scroll unless already at bottom.

### H8. Three disagreeing sources of "who's online"
`/agents` (DB status, online-only) vs `/observer/agents` (all rows, `observer.py:20-25`) vs reality
(`_subs`). After C2's restart all three diverge maximally; `agents_all` also lists every dead identity
as a valid send-as target.

---

## MEDIUM

- **M1. Default identity is `all`, violating spec** (`app.js:10`; spec decision #6 = adam/first agent);
  All-activity unread badge sums every participant's unread (`app.js:22-23`) — a meaningless number.
- **M2. Per-identity unread badges are dead code** — `opt(...,unread)` renders a badge but both call
  sites pass hardcoded `0` (`app.js:47,49`); spec promises per-identity unread in the switcher.
- **M3. Subject derivation diverges** — sidebar uses `WHERE id=thread_id` (`observer.py:49`), open pane
  uses earliest `created_at` (`observer.py:78,91`); ISO-second precision means same-second ties can
  disagree, and a deleted root yields different titles in the two views.
- **M4. Compose always forks a new thread** even when one already exists with that recipient
  (`app.js:155`, `messages.py:39`) — duplicate threads, no "continue existing" affordance.
- **M5. Compose hint/toast over-promise** — "poked in real time… ✓✓ Read once they open it"
  (`app.js:138-139,174`) is false for offline/human/dead recipients.
- **M6. N+1 in hot paths** — `_row_to_out` does a `SELECT address` per message in `inbox`/`thread`
  (`messages.py:14-21`); `observer._summary` runs ~5 queries per thread (`observer.py:48-69`).
- **M7. Missing indexes** on `messages.sender_id` and `messages.created_at` (heavy filter/sort in
  observer queries) — full scans at scale.

## LOW

- **L1.** No guard against self-send or empty body in `send`/`send_as` (`messages.py:33-35`); UI blocks
  it but the unauth API doesn't.
- **L2.** `set_name` UNIQUE race → `IntegrityError` → 500 (same as H4).
- **L3.** `create_identity` returns hardcoded `status="online"` it didn't verify (`observer.py:99`).
- **L4.** Fresh-process SSE resume (`last_id="0"`, `mcp_server.py:111`) re-pokes backlog for a restarted
  *persistent named* identity (non-issue for `copilot-*` with empty backlogs).
- **L5.** Raw server error text surfaced (truncated) into the status bar (`app.js:176`) — via
  `textContent`, so not XSS, but leaks error fragments.

## Verified-correct (ruled out — do not chase)
- Observing a thread does **not** mark messages read — `observer.thread` is read-only (`observer.py:74-93`);
  a sender viewing their own message skips `mark_read` (`messages.py:92-96`). Genuine pass.
- Reply stays in-thread — `_lastIds[tid]` always resolves to a message in the open thread
  (`app.js:120`, `messages.py:40-49`). No wrong-thread bug.
- Live-event refresh does not lose a compose draft — guarded by `!composeMode` (`app.js:185`). (The real
  defect is the scroll/re-render storm, H7.)

---

## Suggested remediation ordering (for discussion — nothing fixed yet)

**Tier 1 — honesty & safety (small, high-value):** C2 startup reconciliation; C6 attribute-escape +
address slug-validation; H4 idempotency catch; redo the receipt/presence UI to tell the truth
(C3/H3/M5) even before the deep model changes.

**Tier 2 — the presence model (the family the maintainer noticed):** liveness-derived presence
(heartbeat/TTL) replacing the latch (C2/H1/H8/H2/L3), and a real definition of Delivered/Read incl. a
human read path (C3/C4).

**Tier 3 — architecture:** C1 connection isolation; H5 atomic message+event; H6 retention + bounded
replay.

**Tier 4 — Observatory correctness:** C5 group-reply, H7 scoped live refresh, M1–M7 spec gaps & perf.

Open scope question for the maintainer: fix breadth-first across all tiers, or go deep on the presence/
delivery model (Tier 2, the reported symptom's true root) first?
