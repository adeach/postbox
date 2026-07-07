# Agent Messaging Platform (codename: Postbox)

Local, self-hosted "email for AI agents" — each agent has an identity + inbox and exchanges async, threaded messages. Primary surfaces: GitHub Copilot CLI and the standalone GitHub Copilot app; Claude Code and Cursor are secondary.

## Workspace Index
- `docs/superpowers/specs/2026-06-29-agent-messaging-platform-design.md` — approved v1 design spec (architecture, data model, API, scope, testing).
- `docs/superpowers/specs/2026-06-29-postbox-v2-session-identity-tmux-wakeup-design.md` — v2 design: per-session auto-identity + real-time tmux wakeup (for review).
- `docs/superpowers/specs/2026-06-30-postbox-observatory-web-ui-design.md` — approved design: Slack-style web Observatory (open-as-any-identity inbox + all-activity + reply-as), backed by observer/global-read + send-as API.
- `docs/superpowers/specs/2026-06-30-postbox-correctness-audit.md` — adversarial audit: 6 Critical / 8 High fundamental correctness bugs (presence latch, no DB isolation, "Delivered" decoupled from liveness, group-reply misroute, stored XSS); 3 root causes + remediation tiers.
- `docs/superpowers/specs/2026-06-30-postbox-presence-delivery-honest-model-design.md` — approved design: live-derived presence (no stored latch/heartbeat), 3-state delivery (Read/Delivered/Queued + human Sent), human read path (`POST /observer/read`, auto-on-open), name reuse vs online holders. Fixes audit Tier 2.
- `docs/superpowers/specs/2026-07-01-postbox-fleet-mode-design.md` — approved design: run dozens of headless agents from the UI; in-process Supervisor spawns turns on new mail (per-identity coalesced, capped, backoff, group-kill), pre-registered token identity (sidesteps H2), `/fleet` API + optional observer token.
- `docs/superpowers/specs/2026-07-06-postbox-federation-design.md` — PROPOSED (not built): `agent@instance` peering between two Postbox servers (email/SMTP-relay model). Stub remote-agent rows (forced by sender/recipient FKs), `thread_id` propagated verbatim (no mapping), `/federation/inbound` (peer-token), presence/receipts degraded to async-mail in v1. Allowlist peering only.
- `mockups/` — web UI design mockups; `8-slack-dropdown.html` is the approved interactive prototype; `9-honest-receipts.html` shows the Tier-2 receipt states + person-vs-agent presence.
- `docs/superpowers/plans/2026-06-29-agent-messaging-platform-v1.md` — v1 implementation plan (Tasks 0–11).
- `README.md` — run/MCP-config/listener/manual-demo instructions.
- `pyproject.toml` — project metadata + deps.
- `CLAUDE.md` — this file; workspace index.
- `postbox/config.py` — Settings loaded from `~/.postbox/config.yaml` (precedence env>yaml>default, null-safe): data dir, db path, host/port, fleet knobs, + federation `instance` name and `peers_seed`.
- `postbox/schema.sql` — DDL for agents/messages/recipients/attachments/events.
- `postbox/db.py` — single shared aiosqlite connection (WAL) + serialized writes.
- `postbox/auth.py` — id/time/token helpers (uuid, iso time, token gen/hash) + UI session-cookie sign/verify (hmac with the password).
- `postbox/models.py` — Pydantic request/response models.
- `postbox/agents.py` — agent service: register, directory, token lookup.
- `postbox/events.py` — event log + in-process bus + race-free SSE replay handoff.
- `postbox/messages.py` — message service: send/inbox/read/thread + idempotency.
- `postbox/api.py` — FastAPI app (`create_app`): REST routes, bearer auth, SSE endpoint, `/observer/*` routes + firehose SSE + static `/ui` mount, `/peers` admin (observer-guarded), and `POST /federation/inbound` (peer-token relay).
- `postbox/observer.py` — ObserverService: global (identity-agnostic) reads (agents, threads, detail) + create-identity + send-as for the web Observatory.
- `postbox/terminals.py` — TerminalService: spawn/list/kill INTERACTIVE copilot sessions in detached tmux sessions (`postbox_<name>`) the human attaches to; server-set POSTBOX_NAME + POSTBOX_URL, injection-safe argv, injectable runner/program seams.
- `postbox/fleet.py` — Fleet mode: `FleetService` (managed-agent registry CRUD + backoff policy) + `Supervisor` (in-process; reconciles on new mail, spawns headless turns via injected `POSTBOX_TOKEN`, coalesced per identity, global cap, crash-loop backoff, process-group kill).
- `postbox/peers.py` — `PeerService`: peers table CRUD + `seed()` (no-clobber) for federation peer registry (`{name,url,token}`).
- `postbox/federation.py` — `parse_address` (`name@instance`) + `FederationService`: send-routing to peers (store-to-stub + injectable relay, soft-success), and inbound delivery (peer-token auth, anti-spoof from-domain, idempotent, `thread_id` propagation).
- `postbox/web/` — vanilla HTML/CSS/JS **human-first Slack-DM** Observatory client (`index.html`, `styles.css`, `app.js`) served at `/ui/`: your-own-identity onboarding, user-search→DM, Direct messages, top-right "Viewing as" impersonation, Fleet panel, live SSE.
- `postbox/main.py` — uvicorn entrypoint (port 8765).
- `postbox/mcp_server.py` — MCP stdio server (`MailTools` + `build_server`) exposing mail tools over REST, incl. `spawn_terminal` (an agent spins up + then messages a new interactive copilot).
- `postbox/listener/wakeups.py` — wakeup strategies: stub, copilot_cli, copilot_app, os_notify.
- `postbox/listener/daemon.py` — SSE client loop → wakeup dispatch (`run_daemon` + `main`).
- `scripts/e2e_verify.py` — live end-to-end proof harness (real uvicorn + MCP client + listener; asserts all 9 designed capabilities).
- `scripts/mcp_verify.py` — drives v2 `postbox.mcp_server` over real token-less stdio MCP (handshake + tools/list incl. `set_name` + auto-registered `copilot-*` identity + deregister) the way Copilot does.
- `scripts/v2_tmux_e2e.py` — real-tmux end-to-end proof (v2): a Session's wakeup loop pokes a live tmux pane when mail arrives.
- `scripts/observer_e2e.py` — live Observatory proof (real uvicorn): seeds agents + a message, asserts observer endpoints reflect it, send-as delivers, and `/ui/` serves.
- `scripts/presence_delivery_e2e.py` — live HTTP/SSE proof of the honest model (8 checks): live presence, Queued vs Delivered vs Sent, human read path + agent-guard, presence drop on SSE close, no ghost-online after restart.
- `scripts/fleet_e2e.py` — live Fleet-mode proof (real uvicorn + a real spawned subprocess): the Supervisor spawns a headless turn that authenticates AS its identity via injected `POSTBOX_TOKEN`, drains its inbox and replies; asserts identity-injection + per-identity coalescing (one turn drains N messages) + clean exit.
- `scripts/remote_spawn_e2e.py` — live proof of CROSS-INSTANCE terminal spawn (laptop asks a peer to spin up a terminal agent; peer uses `POSTBOX_TERMINAL_CMD=sleep` so no real copilot needed).
- `scripts/federation_e2e.py` — live two-instance federation proof: boots TWO real peered servers (separate data dirs/ports/`instance` names), asserts forward relay + reply both ways, shared `thread_id`, and idempotent re-relay.
- `scripts/demo_live.py` — seeded live demo server on :8765 (alice online via held SSE, bob offline, adam human + messages in every receipt state) for clicking the real `/ui/`.
- `tests/conftest.py` — `db` fixture (temp-db Database).
- `tests/test_smoke.py` `test_db.py` `test_auth.py` `test_models.py` `test_agents.py` `test_events.py` `test_messages.py` `test_api.py` `test_sse.py` `test_mcp.py` `test_listener.py` `test_observer.py` `test_observer_api.py` `test_fleet.py` `test_fleet_api.py` `test_config.py` `test_peers.py` `test_peers_api.py` `test_agents_remote.py` `test_federation_addr.py` `test_federation.py` `test_federation_api.py` `test_terminals.py` — per-module test suites.

## Status
v1 service implemented (2026-06-29). REST + SSE service, MCP server, and listener daemon built and tested (39 tests passing).
v2 implemented (2026-06-29): per-session auto-identity (token-less shared MCP config, `set_name`) + real-time tmux wakeup (idle pane poked on new mail).
Observatory implemented (2026-06-30): Slack-themed web UI at `/ui/` (open-as-any-identity inbox, all-activity firehose, reply-as, live SSE) backed by `ObserverService` + `/observer/*` API (70 tests passing).
Fleet mode implemented (2026-07-01): run dozens of headless agents from the UI — in-process `Supervisor` spawns `copilot -p` turns on new mail (per-identity coalesced, global cap, crash-loop backoff, process-group kill), each authenticating AS its durable identity via injected `POSTBOX_TOKEN`; `/fleet` API + 🤖 Fleet tab + optional `POSTBOX_OBSERVER_TOKEN` guard (99 tests + live `scripts/fleet_e2e.py`; code-reviewed, 4 findings fixed). On branch `feat/fleet-mode`.
Observatory redesigned (2026-07-03): **human-first Slack-style DMs** — you join as your own identity; sidebar user-search → DM anyone; **Direct messages** list (Slack rows, no channels); top-right **"Viewing as"** impersonates an agent to see *its* conversations and send on its behalf (mark-read stays human-only); Fleet panel; live SSE. Removed the "open-as-anyone" switcher, All-activity firehose, and fake search/toolbar. Frontend-only rewrite reusing `/observer/*` + `/fleet` (100 tests still passing). Approved mock: `mockups/12-slack-dm.html`. On branch `feat/fleet-mode`.
Federation v1 implemented (2026-07-06): `agent@instance` peering between Postbox servers (email/relay model). `~/.postbox/config.yaml` config (instance name + peers); `peers` table + `/peers` admin API; `parse_address` + stub remote-agent rows; send-routing (store-to-stub + relay, soft-success) + `POST /federation/inbound` (peer-token auth, anti-spoof from-domain, idempotent, `thread_id` propagated); UI first-contact + remote badge. Direct 1:1 peering; presence/receipts degrade to async-mail. gpt-5.5 subagents wrote each stage; reviewed + committed per stage. 132 unit tests + live `scripts/federation_e2e.py` (two real peered servers, both-way relay). On branch `feat/federation`.

Agent management implemented (2026-07-07): **resumable identities + UI forget**. Each self-registering session records its `COPILOT_AGENT_SESSION_ID` (new `agents.session_key`, unique); register is now register-or-reattach — a resumed session (`copilot --resume <id|name>`) rebinds to the SAME identity (inbox/threads intact, token rotated) instead of a fresh `copilot-*`. Session-key identities PERSIST on MCP exit (listed offline, resumable) instead of deregistering; the Fleet directory shows each agent's session id (copy chip) and a ✕ **Forget** button (`DELETE /observer/agents/{id}` → soft deregister: hides it, KEEPS messages/session). No env var; naming stays via `set_name`. 139 tests + live UI/API verify. On branch `feat/agent-mgmt`.

Terminal agents implemented (2026-07-07): **spin up INTERACTIVE copilot from the UI/API**. `TerminalService` + `/terminals` API (POST spawn / GET list / DELETE kill, observer-guarded) creates a detached tmux session `postbox_<name>` running `copilot` (server-set `POSTBOX_NAME` + `POSTBOX_URL` via injection-safe `env` argv), and the UI hands you the `tmux attach -t …` command. Running inside tmux → the agent auto-registers, gets a resumable session id + real-time mail poke for free. Fleet page gains a "Spin up terminal" bar + "Terminal agents" list (copy-attach + kill). Complements headless Fleet (Supervisor-driven) — same registration, different trigger. tmux is the bridge (a web server has no TTY); session lives on whatever host runs Postbox (attach locally or over ssh). 154 tests + real-tmux spawn/list/kill smoke + live UI verify. On branch `feat/agent-mgmt`.

## Stack
Python + FastAPI (REST + SSE via sse-starlette), SQLite (WAL) via aiosqlite, official Python MCP SDK, httpx + httpx-sse (client side). Single `uvicorn` process.

## Conventions
- Inbox (SQLite) is the durable source of truth; SSE + listener daemon is best-effort wakeup.
- Monotonic `events.id` is the sole ordering authority for SSE replay.
- Commit after each meaningful change (one-line message, no model name).
