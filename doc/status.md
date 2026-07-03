# Status — Postbox

## Current goal — DONE
Human-first, Slack-style DM Observatory (approved mock: `mockups/12-slack-dm.html`).
Implemented **frontend-only**, live-verified against the real backend. 100 backend tests pass.

## Branch / worktree
Merged into `main` in the main repo (2026-07-03); the `.worktrees/fleet-mode` worktree was closed. Branch `feat/fleet-mode` still exists (== main). NOT pushed.

## What shipped (commits from 9e40247)
- Stage 1 — markup + styles (Slack-DM layout).
- Stage 2 — core: you-identity onboarding (localStorage + `POST /observer/identity`), DM list
  (`/observer/threads?address=you`), open/send (`/observer/send`), honest receipts, human
  mark-read (`/observer/read`), live SSE (`/observer/events`).
- Stage 3 — sidebar user search -> open or draft a DM (first send creates the thread).
- Stage 4 — top-right "Viewing as" impersonation (see that agent's conversations, send-as);
  mark-read stays human-only.
- Stage 5 — Fleet panel wired to `/fleet` (list + 2s poll, add/run/kill/enable/disable/remove).
- Stage 6 — dead-CSS prune, README + CLAUDE.md + this file + backlog, live e2e verify.

## Run / verify
- Server (worktree): `cd .worktrees/fleet-mode && ../../.venv/bin/python -m postbox.main`
- UI: http://127.0.0.1:8765/ui/  .  Tests: `PYTHONPATH=$(pwd) ../../.venv/bin/python -m pytest -q` (100 pass)
- Backend UNCHANGED — reused `/observer/*` + `/fleet`.

## Next / open
- Not pushed, no PR (awaiting the maintainer's go).
- Follow-ups in `doc/backlog.md` (onboarding picker, thread dedupe, server-side search).
