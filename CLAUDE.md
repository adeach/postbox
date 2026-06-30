# Agent Messaging Platform (codename: Postbox)

Local, self-hosted "email for AI agents" — each agent has an identity + inbox and exchanges async, threaded messages. Primary surfaces: GitHub Copilot CLI and the standalone GitHub Copilot app; Claude Code and Cursor are secondary.

## Workspace Index
- `docs/superpowers/specs/2026-06-29-agent-messaging-platform-design.md` — approved v1 design spec (architecture, data model, API, scope, testing).
- `docs/superpowers/specs/2026-06-29-postbox-v2-session-identity-tmux-wakeup-design.md` — v2 design: per-session auto-identity + real-time tmux wakeup (for review).
- `docs/superpowers/specs/2026-06-30-postbox-observatory-web-ui-design.md` — approved design: Slack-style web Observatory (open-as-any-identity inbox + all-activity + reply-as), backed by observer/global-read + send-as API.
- `mockups/` — web UI design mockups; `8-slack-dropdown.html` is the approved interactive prototype.
- `docs/superpowers/plans/2026-06-29-agent-messaging-platform-v1.md` — v1 implementation plan (Tasks 0–11).
- `README.md` — run/MCP-config/listener/manual-demo instructions.
- `pyproject.toml` — project metadata + deps.
- `CLAUDE.md` — this file; workspace index.
- `postbox/config.py` — Settings (data dir, db path, host/port).
- `postbox/schema.sql` — DDL for agents/messages/recipients/attachments/events.
- `postbox/db.py` — single shared aiosqlite connection (WAL) + serialized writes.
- `postbox/auth.py` — id/time/token helpers (uuid, iso time, token gen/hash).
- `postbox/models.py` — Pydantic request/response models.
- `postbox/agents.py` — agent service: register, directory, token lookup.
- `postbox/events.py` — event log + in-process bus + race-free SSE replay handoff.
- `postbox/messages.py` — message service: send/inbox/read/thread + idempotency.
- `postbox/api.py` — FastAPI app (`create_app`): REST routes, bearer auth, SSE endpoint, `/observer/*` routes + firehose SSE + static `/ui` mount.
- `postbox/observer.py` — ObserverService: global (identity-agnostic) reads (agents, threads, detail) + create-identity + send-as for the web Observatory.
- `postbox/web/` — vanilla HTML/CSS/JS Slack-themed Observatory client (`index.html`, `styles.css`, `app.js`) served at `/ui/`.
- `postbox/main.py` — uvicorn entrypoint (port 8765).
- `postbox/mcp_server.py` — MCP stdio server (`MailTools` + `build_server`) exposing mail tools over REST.
- `postbox/listener/wakeups.py` — wakeup strategies: stub, copilot_cli, copilot_app, os_notify.
- `postbox/listener/daemon.py` — SSE client loop → wakeup dispatch (`run_daemon` + `main`).
- `scripts/e2e_verify.py` — live end-to-end proof harness (real uvicorn + MCP client + listener; asserts all 9 designed capabilities).
- `scripts/mcp_verify.py` — drives v2 `postbox.mcp_server` over real token-less stdio MCP (handshake + tools/list incl. `set_name` + auto-registered `copilot-*` identity + deregister) the way Copilot does.
- `scripts/v2_tmux_e2e.py` — real-tmux end-to-end proof (v2): a Session's wakeup loop pokes a live tmux pane when mail arrives.
- `scripts/observer_e2e.py` — live Observatory proof (real uvicorn): seeds agents + a message, asserts observer endpoints reflect it, send-as delivers, and `/ui/` serves.
- `tests/conftest.py` — `db` fixture (temp-db Database).
- `tests/test_smoke.py` `test_db.py` `test_auth.py` `test_models.py` `test_agents.py` `test_events.py` `test_messages.py` `test_api.py` `test_sse.py` `test_mcp.py` `test_listener.py` `test_observer.py` `test_observer_api.py` — per-module test suites.

## Status
v1 service implemented (2026-06-29). REST + SSE service, MCP server, and listener daemon built and tested (39 tests passing).
v2 implemented (2026-06-29): per-session auto-identity (token-less shared MCP config, `set_name`) + real-time tmux wakeup (idle pane poked on new mail).
Observatory implemented (2026-06-30): Slack-themed web UI at `/ui/` (open-as-any-identity inbox, all-activity firehose, reply-as, live SSE) backed by `ObserverService` + `/observer/*` API (70 tests passing).

## Stack
Python + FastAPI (REST + SSE via sse-starlette), SQLite (WAL) via aiosqlite, official Python MCP SDK, httpx + httpx-sse (client side). Single `uvicorn` process.

## Conventions
- Inbox (SQLite) is the durable source of truth; SSE + listener daemon is best-effort wakeup.
- Monotonic `events.id` is the sole ordering authority for SSE replay.
- Commit after each meaningful change (one-line message, no model name).
