# Agent Messaging Platform (codename: Courier)

Local, self-hosted "email for AI agents" — each agent has an identity + inbox and exchanges async, threaded messages. Primary surfaces: GitHub Copilot CLI and the standalone GitHub Copilot app; Claude Code and Cursor are secondary.

## Workspace Index
- `docs/superpowers/specs/2026-06-29-agent-messaging-platform-design.md` — approved design spec (architecture, data model, API, scope, testing).
- `CLAUDE.md` — this file; workspace index.

## Status
Design approved (2026-06-29). Next: implementation plan, then v1 build. No application code yet.

## Stack (planned)
Python + FastAPI (REST + SSE via sse-starlette), SQLite (WAL) via aiosqlite, official Python MCP SDK. Single `uvicorn` process.

## Conventions
- Inbox (SQLite) is the durable source of truth; SSE + listener daemon is best-effort wakeup.
- Monotonic `events.id` is the sole ordering authority for SSE replay.
- Commit after each meaningful change (one-line message, no model name).
