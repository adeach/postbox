# Agent Messaging Platform (codename: Courier)

Local, self-hosted "email for AI agents" — each agent has an identity + inbox and exchanges async, threaded messages. Primary surfaces: GitHub Copilot CLI and the standalone GitHub Copilot app; Claude Code and Cursor are secondary.

## Workspace Index
- `docs/superpowers/specs/2026-06-29-agent-messaging-platform-design.md` — approved design spec (architecture, data model, API, scope, testing).
- `docs/superpowers/plans/2026-06-29-agent-messaging-platform-v1.md` — v1 implementation plan (Tasks 0–11).
- `README.md` — run/MCP-config/listener/manual-demo instructions.
- `pyproject.toml` — project metadata + deps.
- `CLAUDE.md` — this file; workspace index.
- `courier/config.py` — Settings (data dir, db path, host/port).
- `courier/schema.sql` — DDL for agents/messages/recipients/attachments/events.
- `courier/db.py` — single shared aiosqlite connection (WAL) + serialized writes.
- `courier/auth.py` — id/time/token helpers (uuid, iso time, token gen/hash).
- `courier/models.py` — Pydantic request/response models.
- `courier/agents.py` — agent service: register, directory, token lookup.
- `courier/events.py` — event log + in-process bus + race-free SSE replay handoff.
- `courier/messages.py` — message service: send/inbox/read/thread + idempotency.
- `courier/api.py` — FastAPI app (`create_app`): REST routes, bearer auth, SSE endpoint.
- `courier/main.py` — uvicorn entrypoint (port 8765).
- `courier/mcp_server.py` — MCP stdio server (`MailTools` + `build_server`) exposing mail tools over REST.
- `courier/listener/wakeups.py` — wakeup strategies: stub, copilot_cli, copilot_app, os_notify.
- `courier/listener/daemon.py` — SSE client loop → wakeup dispatch (`run_daemon` + `main`).
- `scripts/e2e_verify.py` — live end-to-end proof harness (real uvicorn + MCP client + listener; asserts all 9 designed capabilities).
- `tests/conftest.py` — `db` fixture (temp-db Database).
- `tests/test_smoke.py` `test_db.py` `test_auth.py` `test_models.py` `test_agents.py` `test_events.py` `test_messages.py` `test_api.py` `test_sse.py` `test_mcp.py` `test_listener.py` — per-module test suites.

## Status
v1 service implemented (2026-06-29). REST + SSE service, MCP server, and listener daemon built and tested (39 tests passing).

## Stack
Python + FastAPI (REST + SSE via sse-starlette), SQLite (WAL) via aiosqlite, official Python MCP SDK, httpx + httpx-sse (client side). Single `uvicorn` process.

## Conventions
- Inbox (SQLite) is the durable source of truth; SSE + listener daemon is best-effort wakeup.
- Monotonic `events.id` is the sole ordering authority for SSE replay.
- Commit after each meaningful change (one-line message, no model name).
