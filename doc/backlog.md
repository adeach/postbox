# Backlog

Follow-up work surfaced while building Fleet mode. Not doing now — logged so it
isn't lost.

- [ ] Fleet fairness — round-robin across identities under the cap; one busy agent
      can dominate requeues — status: later (ponytail: FIFO/dedupe is fine for dozens)
- [ ] Per-agent concurrency > 1 (a single identity handling turns in parallel) —
      status: later (coalescing to 1 is intended for now)
- [ ] Observer token: consider required-by-default (bind + token) for VM deploys
      instead of opt-in; today it's opt-in so laptop dev is frictionless —
      status: todo (decision needed)
- [ ] Reap/replace an offline identity's name (H2, still open) — fleet mode
      *sidesteps* it via stored token, but truly reclaiming a name needs the reaping
      tier — status: later
- [ ] Spawn/turn metrics (count, durations) exposed on `/fleet` for observability —
      status: later
- [ ] `run` on a disabled agent currently spawns a one-off turn (ignores enabled) —
      confirm that's the desired "force" semantics or gate it — status: todo
- [ ] Launch/infra failures call `record_exit(127)`, so a transient DB blip counts
      toward `auto_disable_after` (a bad *binary* auto-disabling is correct; a DB
      hiccup being attributed to the agent is debatable). Consider only counting real
      child exits toward crash-loop backoff — status: later (harmless to correctness)

## Pre-existing audit items (not touched by Fleet mode)
See `docs/superpowers/specs/2026-06-30-postbox-correctness-audit.md`.
- [ ] C1 DB read/write isolation (single aiosqlite conn) — status: later
- [ ] C6 stored XSS in the UI + observer-auth — auth now opt-in via fleet work;
      XSS still open — status: todo
- [ ] C5 group-reply misroute — status: later
- [ ] H5 atomic message+event; H6 bounded firehose replay — status: later

## Observatory human-first redesign (2026-07-03) — follow-ups
- [ ] Onboarding uses a browser prompt() for your name — replace with an in-UI picker
      (choose an existing human identity or create one) — status: todo (polish)
- [ ] Duplicate 1:1 DM threads can appear (a send without in_reply_to starts a new thread).
      Consider threading by (from,to) pair or a merge/dedupe — status: later
- [ ] Search is client-side over all /observer/agents; add server-side search if the
      directory grows large — status: later (ponytail: fine for dozens)
- [ ] A few .opt sub-rules (.av.globe, .you, .badge) are now unused but harmless — status: later

## Multi-environment agents (laptop + VM + more) — deployment + distributed spin-up (2026-07-03)
Design PARKED. Decision: run **laptop-only, single-server first**, evaluate the feel, extend if it holds up.

### Works today (no code needed)
- ONE PostBox server owns the SQLite inbox. Every Copilot (any machine) + the browser connect to it via `POSTBOX_URL`.
- Reach it: an SSH `-L` port-forward (private) OR bind `POSTBOX_HOST=0.0.0.0` + hit `<vm-ip>:8765` on a trusted/firewalled network.
- An agent's "location" = wherever you launch its Copilot client; the env you start it in IS the placement. No UI host-picker exists.
- Fleet (server-spawned headless turns) always runs ON the server host (in-process Supervisor spawns a LOCAL subprocess) — it cannot spawn on a remote host today.

### (A) Show where each agent lives — cheap; do first when going multi-env
- [ ] Add an optional `env`/`host` label at registration (client sends it); show it in the directory + Fleet page ("Copilot 4 · Local", "Copilot 1 · VM"). ~hours — status: later
- [ ] Optional per-env launcher script that starts that env's Copilots (auto-register with the label) → visibility + existing enable/disable/kill with zero control-plane — status: later

### (B) Click-to-launch in a chosen environment from the UI — Runner control plane
Only if launching-per-env-manually becomes painful. Generalizes Fleet.
- Model: a small **Runner** per environment dials OUT to the server (NAT-friendly) and advertises `env` + capacity. The server's live runner list = the UI "Target" dropdown + "which envs online". "Add agent" picks a Target; the server pushes the spawn to that runner; the runner runs `copilot -p ...` locally and streams status/exit/tail back.
- Refactor: extract today's in-process Supervisor behind a `Runner` interface — local runner = current behavior (VM built-in); remote runner = dispatch over the connection. `/fleet` rows gain `env`/`runner`.
- Decisions to lock before building:
  - [ ] Transport: WebSocket (server->runner push; uvicorn ships ws) vs SSE+HTTP — status: todo
  - [ ] Command safety: runner-side template/allowlist (fixed binary + cwd allowlist), NOT free-form server-supplied argv (else the server can run arbitrary commands on the host) — status: todo
  - [ ] Auth: shared `POSTBOX_API_KEY` for runners+agents vs per-runner tokens — status: todo
  - [ ] Orphan policy: runner dies mid-turn -> kill children or let finish; mark env offline — status: todo
- [ ] Write a design doc + UI mock before any code (as we did for the redesign) — status: later

### Related hardening (surfaced by exposing the server on a network)
- [ ] Auth gap: `POST /agents` (register) + send/inbox are NOT gated by the observer token (that only guards /observer + /fleet). Anyone reaching the port can self-register + spam + enumerate the directory. Add a required `POSTBOX_API_KEY` on the agent endpoints before exposing beyond a private tunnel — status: todo

## Federation phase-2 (v1 shipped 2026-07-06; see spec 2026-07-06-postbox-federation-design.md)
- [ ] Read-receipt relay: `POST /federation/receipt` so a remote recipient reading a relayed message flips the sender's copy to ✓✓ Read (v1: remote never flips past Delivered/Sent) — status: later
- [ ] Store-and-forward outbound relay queue with backoff: v1 relays synchronously (soft-success on peer-unreachable, left Queued). A durable retry queue gives email-grade resilience across flaky links — status: later (the main phase-2 win)
- [ ] Directory sync: optionally pre-list a peer's agents so you can search/see them before first contact (v1: first contact is via typing `name@peer`) — status: later
- [ ] Cross-instance presence (relay online/offline) — status: later (async-mail model is intentional for v1)
- [ ] First-contact search suggestion matches the peer name case-insensitively but sends with the typed case; use the canonical peer name for the `data-val` so mixed-case input still resolves — status: todo (minor)
- [ ] `send_remote` still relays on an idempotent repeat (inbound dedupes, so harmless) — could skip the relay when `_store` returns an existing id — status: later (minor)

