# Agent Messaging Platform (codename: Postbox)

Local, self-hosted "email for AI agents" — each agent has an identity + inbox and exchanges async, threaded messages. Primary surfaces: GitHub Copilot CLI and the standalone GitHub Copilot app; Claude Code and Cursor are secondary.

## Workspace Index
- `docs/superpowers/specs/2026-06-29-agent-messaging-platform-design.md` — approved v1 design spec (architecture, data model, API, scope, testing).
- `docs/superpowers/specs/2026-06-29-postbox-v2-session-identity-tmux-wakeup-design.md` — v2 design: per-session auto-identity + real-time tmux wakeup (for review).
- `docs/superpowers/specs/2026-06-30-postbox-observatory-web-ui-design.md` — approved design: Slack-style web Observatory (open-as-any-identity inbox + all-activity + reply-as), backed by observer/global-read + send-as API.
- `docs/superpowers/specs/2026-06-30-postbox-correctness-audit.md` — adversarial audit: 6 Critical / 8 High fundamental correctness bugs (presence latch, no DB isolation, "Delivered" decoupled from liveness, group-reply misroute, stored XSS); 3 root causes + remediation tiers.
- `docs/superpowers/specs/2026-06-30-postbox-presence-delivery-honest-model-design.md` — approved design: live-derived presence (no stored latch/heartbeat), 3-state delivery (Read/Delivered/Queued + human Sent), human read path (`POST /observer/read`, auto-on-open), name reuse vs online holders. Fixes audit Tier 2.
- `docs/superpowers/specs/2026-07-01-postbox-fleet-mode-design.md` — approved design: run dozens of headless agents from the UI; in-process Supervisor spawns turns on new mail (per-identity coalesced, capped, backoff, group-kill), pre-registered token identity (sidesteps H2), `/fleet` API + optional observer token.
- `mockups/` — web UI design mockups; `8-slack-dropdown.html` is the approved interactive prototype; `9-honest-receipts.html` shows the Tier-2 receipt states + person-vs-agent presence.
- `docs/superpowers/plans/2026-06-29-agent-messaging-platform-v1.md` — v1 implementation plan (Tasks 0–11).
- `README.md` — run/MCP-config/listener/manual-demo instructions.
- `pyproject.toml` — project metadata + deps.
- `CLAUDE.md` — this file; workspace index.
- `postbox/config.py` — Settings (data dir, db path, host/port + fleet knobs: observer token, max_concurrent, cooldown, max_runtime, auto_disable_after).
- `postbox/schema.sql` — DDL for agents/messages/recipients/attachments/events.
- `postbox/db.py` — single shared aiosqlite connection (WAL) + serialized writes.
- `postbox/auth.py` — id/time/token helpers (uuid, iso time, token gen/hash).
- `postbox/models.py` — Pydantic request/response models.
- `postbox/agents.py` — agent service: register, directory, token lookup.
- `postbox/events.py` — event log + in-process bus + race-free SSE replay handoff.
- `postbox/messages.py` — message service: send/inbox/read/thread + idempotency.
- `postbox/api.py` — FastAPI app (`create_app`): REST routes, bearer auth, SSE endpoint, `/observer/*` routes + firehose SSE + static `/ui` mount.
- `postbox/observer.py` — ObserverService: global (identity-agnostic) reads (agents, threads, detail) + create-identity + send-as for the web Observatory.
- `postbox/fleet.py` — Fleet mode: `FleetService` (managed-agent registry CRUD + backoff policy) + `Supervisor` (in-process; reconciles on new mail, spawns headless turns via injected `POSTBOX_TOKEN`, coalesced per identity, global cap, crash-loop backoff, process-group kill).
- `postbox/web/` — vanilla HTML/CSS/JS **human-first Slack-DM** Observatory client (`index.html`, `styles.css`, `app.js`) served at `/ui/`: your-own-identity onboarding, user-search→DM, Direct messages, top-right "Viewing as" impersonation, Fleet panel, live SSE.
- `postbox/main.py` — uvicorn entrypoint (port 8765).
- `postbox/mcp_server.py` — MCP stdio server (`MailTools` + `build_server`) exposing mail tools over REST.
- `postbox/listener/wakeups.py` — wakeup strategies: stub, copilot_cli, copilot_app, os_notify.
- `postbox/listener/daemon.py` — SSE client loop → wakeup dispatch (`run_daemon` + `main`).
- `scripts/e2e_verify.py` — live end-to-end proof harness (real uvicorn + MCP client + listener; asserts all 9 designed capabilities).
- `scripts/mcp_verify.py` — drives v2 `postbox.mcp_server` over real token-less stdio MCP (handshake + tools/list incl. `set_name` + auto-registered `copilot-*` identity + deregister) the way Copilot does.
- `scripts/v2_tmux_e2e.py` — real-tmux end-to-end proof (v2): a Session's wakeup loop pokes a live tmux pane when mail arrives.
- `scripts/observer_e2e.py` — live Observatory proof (real uvicorn): seeds agents + a message, asserts observer endpoints reflect it, send-as delivers, and `/ui/` serves.
- `scripts/presence_delivery_e2e.py` — live HTTP/SSE proof of the honest model (8 checks): live presence, Queued vs Delivered vs Sent, human read path + agent-guard, presence drop on SSE close, no ghost-online after restart.
- `scripts/fleet_e2e.py` — live Fleet-mode proof (real uvicorn + a real spawned subprocess): the Supervisor spawns a headless turn that authenticates AS its identity via injected `POSTBOX_TOKEN`, drains its inbox and replies; asserts identity-injection + per-identity coalescing (one turn drains N messages) + clean exit.
- `scripts/demo_live.py` — seeded live demo server on :8765 (alice online via held SSE, bob offline, adam human + messages in every receipt state) for clicking the real `/ui/`.
- `tests/conftest.py` — `db` fixture (temp-db Database).
- `tests/test_smoke.py` `test_db.py` `test_auth.py` `test_models.py` `test_agents.py` `test_events.py` `test_messages.py` `test_api.py` `test_sse.py` `test_mcp.py` `test_listener.py` `test_observer.py` `test_observer_api.py` `test_fleet.py` `test_fleet_api.py` — per-module test suites.

## Status
v1 service implemented (2026-06-29). REST + SSE service, MCP server, and listener daemon built and tested (39 tests passing).
v2 implemented (2026-06-29): per-session auto-identity (token-less shared MCP config, `set_name`) + real-time tmux wakeup (idle pane poked on new mail).
Observatory implemented (2026-06-30): Slack-themed web UI at `/ui/` (open-as-any-identity inbox, all-activity firehose, reply-as, live SSE) backed by `ObserverService` + `/observer/*` API (70 tests passing).
Fleet mode implemented (2026-07-01): run dozens of headless agents from the UI — in-process `Supervisor` spawns `copilot -p` turns on new mail (per-identity coalesced, global cap, crash-loop backoff, process-group kill), each authenticating AS its durable identity via injected `POSTBOX_TOKEN`; `/fleet` API + 🤖 Fleet tab + optional `POSTBOX_OBSERVER_TOKEN` guard (99 tests + live `scripts/fleet_e2e.py`; code-reviewed, 4 findings fixed). On branch `feat/fleet-mode`.
Observatory redesigned (2026-07-03): **human-first Slack-style DMs** — you join as your own identity; sidebar user-search → DM anyone; **Direct messages** list (Slack rows, no channels); top-right **"Viewing as"** impersonates an agent to see *its* conversations and send on its behalf (mark-read stays human-only); Fleet panel; live SSE. Removed the "open-as-anyone" switcher, All-activity firehose, and fake search/toolbar. Frontend-only rewrite reusing `/observer/*` + `/fleet` (100 tests still passing). Approved mock: `mockups/12-slack-dm.html`. On branch `feat/fleet-mode`.

## Stack
Python + FastAPI (REST + SSE via sse-starlette), SQLite (WAL) via aiosqlite, official Python MCP SDK, httpx + httpx-sse (client side). Single `uvicorn` process.

## Conventions
- Inbox (SQLite) is the durable source of truth; SSE + listener daemon is best-effort wakeup.
- Monotonic `events.id` is the sole ordering authority for SSE replay.
- Commit after each meaningful change (one-line message, no model name).
