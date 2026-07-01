# Postbox — Fleet Mode Design (2026-07-01)

Run Postbox in one place (laptop, then a VM) and manage a **fleet of dozens of
headless agents entirely from the UI**. Agents don't sit idle in panes; a
supervisor spawns a headless turn **on demand when mail arrives**, coalesced per
identity, capped, with crash-loop backoff. The Observatory gains a **Fleet tab**
to add/enable/disable/run/kill agents and watch their turn status.

Approved shape (2026-07-01). Supersedes the tmux-pane wakeup for the
many-agents case; tmux stays for the hand-watched few.

## Why this shape
- Turn-based agents can't be interrupted. **Spawn-on-arrival** (`copilot -p "…"`)
  is the scalable "wakeup": no resident context per agent, spawn only when there's
  work. (Original design's "strategy 2".)
- The **durable inbox is the source of truth**; SSE events are only *hints*. The
  scheduler never spawns from an event — it `reconcile()`s against current unread
  state. This makes dropped/over-cap/replayed events harmless.
- Deployment is unchanged: one uvicorn process. Laptop → browser hits
  `localhost:8765`. VM → `ssh -L 8765:localhost:8765`; only the browser crosses
  the forward, the fleet is VM-local.

## Identity: pre-registered token (sidesteps the open H2 name-reuse bug)
A fleet agent **is** a durable registered identity. Adding "alice" to the fleet
registers her **once** and stores her token. Every headless turn is spawned with
`POSTBOX_TOKEN=<alice's token>`, so it authenticates **as alice** — no
`set_name`, no re-registration, no `address UNIQUE` collision. The MCP server, if
`POSTBOX_TOKEN` is set, skips auto-register **and** skips deregister-on-exit (the
identity is durable, not an ephemeral session). H2 is thus avoided, not fixed.

Presence stays honest: a one-shot turn holds an SSE sub only while it runs, so
alice shows "online" during her turn and offline otherwise — the mail dots.
**Fleet turn-status (idle/running/queued/disabled) is a separate axis** shown in
the Fleet tab; it is never conflated with mail-presence.

## Data model
New table (additive; `CREATE TABLE IF NOT EXISTS` in `schema.sql`):
```
fleet_agents(
  address       TEXT PRIMARY KEY,   -- == the agent identity's address
  token         TEXT NOT NULL,      -- plaintext; local secret in a local DB
  command_json  TEXT NOT NULL,      -- JSON arg-list template, {prompt} placeholder
  cwd           TEXT,               -- where the headless CLI runs (has the MCP config)
  enabled       INTEGER NOT NULL DEFAULT 1,
  fail_count    INTEGER NOT NULL DEFAULT 0,
  backoff_until TEXT,               -- iso; reconcile skips until then
  last_exit     INTEGER,            -- last turn exit code
  last_run      TEXT,               -- iso
  created_at    TEXT NOT NULL
)
```
Default `command_json`: `["copilot","-p","{prompt}"]`. Arg-list, never a shell
string → no command injection.

## Scheduler (the engine): reconcile, not per-message
State: `running: set[address]`, a global `asyncio.Semaphore(max_concurrent)`, a
single reconcile loop woken by an `asyncio.Event`.

`reconcile()` (the only path that spawns):
1. Query **current** unread: enabled fleet agents that have ≥1 unread message,
   are not already `running`, and whose `backoff_until`/`last_run+cooldown` have
   passed.
2. For each such address, if a semaphore slot is free → spawn a turn; else leave
   it (durable — next reconcile picks it up).

Triggers that just wake the loop (never spawn directly):
- **In-process firehose** consumer (`bus.subscribe_all()`) on `message.received`.
- **Periodic** safety-net every `reconcile_interval` (e.g. 20s).
- **On turn exit**.

Coalescing: one running slot per address ⇒ 5 messages to alice = **1 turn** that
drains all her unread. Prompt is generic: *"You have unread Postbox mail — check
your inbox and handle it."*

Replay herd: supervisor starts the firehose from **now** (current max event id)
and does one startup `reconcile()`; it never replays history into spawns.

Crash-loop guard: on exit, `exit==0` → `fail_count=0`; `exit!=0` →
`fail_count++`, `backoff_until = now + min(cap, base·2^fail_count)`,
auto-disable when `fail_count >= auto_disable_after`. A per-agent `cooldown`
after every turn bounds even an exit-0 no-op loop.
*(ponytail: FIFO/dedupe scheduling; round-robin only if one agent starves others.)*

## Process hygiene
`create_subprocess_exec(*argv, cwd, env={POSTBOX_TOKEN, POSTBOX_URL}, stdout=PIPE,
stderr=STDOUT, start_new_session=True)`:
- own process group (`start_new_session`) → **kill the group** (SIGTERM→SIGKILL).
- async-drained bounded output tail (last N lines) — no pipe deadlock, no unbounded memory.
- per-turn max runtime watchdog → group-kill on overrun.
- supervisor shutdown group-kills all running turns.
*(ponytail: supervisor runs in-process with the server; split to its own process
only if hygiene bugs threaten the server.)*

## API (`/fleet/*`)
- `GET  /fleet` — list agents + live status `{address, enabled, state, last_exit, last_run, fail_count, tail}` where `state ∈ idle|running|queued|disabled|backoff`.
- `POST /fleet` — add/update `{address, command?, cwd?}` (registers the identity if new, stores token).
- `POST /fleet/{addr}/enable` · `/disable` · `/run` (force a turn now) · `/kill` (group-kill in-flight turn).
- `DELETE /fleet/{addr}` — remove from fleet (identity row stays).

## Security
`POSTBOX_OBSERVER_TOKEN` (env). When set, `/observer/*`, `/fleet/*` and the
firehose require it (`X-Observer-Token` header, or `?token=` for `EventSource`,
which can't set headers). When **unset**, endpoints are open and the server binds
`127.0.0.1` only — the localhost/SSH-forward boundary is the gate (frictionless
laptop dev). Commands are allow-listed arg-lists; `cwd` validated to an existing
dir. This is the C6 fix, opt-in so laptop-first testing isn't blocked; **turn it
on for the VM.**

## Scope
**In:** table + models, `FleetService` (CRUD) + `Supervisor` (reconcile/spawn/
hygiene/backoff), `POSTBOX_TOKEN` in MCP, `/fleet` API + optional observer-token,
Fleet tab, unit tests, a live e2e proof (stub agent command using the injected
token to read+reply — proves the loop without real `copilot`).

**Cut for v1 (ponytail):** per-message prompts, per-thread routing, round-robin
fairness, separate supervisor process, richer retry policy.

## Testing
- `tests/test_fleet.py` (stub runner, no real subprocess): CRUD; reconcile
  coalesces per-address; respects the concurrency cap (queues the rest);
  backoff+auto-disable on repeated failure; group-kill terminates a turn;
  disabled/backoff agents are skipped.
- `scripts/fleet_e2e.py` (real uvicorn + a real stub-agent subprocess that reads
  `POSTBOX_TOKEN` and replies over REST): send mail to a fleet agent → assert a
  turn spawns, the reply arrives **from that identity**, coalescing holds, and an
  over-cap message drains after a slot frees.
