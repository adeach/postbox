# Status — Postbox Fleet mode

## Current goal
Run Postbox with a fleet of dozens of headless agents managed from the UI
(laptop first, then a VM over an SSH port-forward). **Implemented and green.**

## Branch / worktree
- Branch: `feat/fleet-mode`
- Worktree: `.worktrees/fleet-mode` (all work here; not on `main`)

## Done
- Spec: `docs/superpowers/specs/2026-07-01-postbox-fleet-mode-design.md`
- `fleet_agents` table (`postbox/schema.sql`); models `FleetAgentIn/Out`.
- `postbox/fleet.py`: `FleetService` (CRUD + backoff policy) + `Supervisor`
  (in-process; reconcile-on-mail, per-identity coalesce, global cap, crash-loop
  backoff + auto-disable, process-group kill, bounded output tail).
- Identity injection: `postbox/mcp_server.py` honors `POSTBOX_TOKEN` (durable
  identity: no register/deregister). Sidesteps the open H2 name-reuse bug.
- `/fleet` REST API + `require_observer` guard (opt-in `POSTBOX_OBSERVER_TOKEN`),
  supervisor wired into the app lifespan (`postbox/api.py`).
- 🤖 Fleet tab in the Observatory (`postbox/web/*`), incl. observer-token support.
- Tests: `tests/test_fleet.py` (14, incl. real group-kill + 4 code-review regressions:
  concurrent-run_now single-spawn, huge-output reap+bound, stop() awaits turns) and
  `tests/test_fleet_api.py` (4). Full suite **99 passing**.
- Live proof: `scripts/fleet_e2e.py` — real subprocess authenticates via injected
  token, coalescing verified. Passes (re-verified after review fixes).
- README "Fleet mode" section (add agents, VM port-forward, tunables).
- **Code review: complete & clean** (background agent, 3 rounds). Round 1: 1 Critical
  (double-spawn) + 1 High (huge-line orphan) + 2 Medium (stop() task leak, set_name
  500). Round 2: 2 new in the reworked path (reservation leak on DB error; late task
  on stop). Round 3: cancellation-path reservation leak. **All fixed + regression-
  tested; reviewer confirmed the reserve/launch/stop path sound, nothing outstanding.**

## Next step
- User tests on laptop (swap command to real `copilot -p {prompt}`), then VM.
- NOT pushed / no PR (waiting for the maintainer's go).

## Key paths
- Core: `postbox/fleet.py` · API: `postbox/api.py` (`/fleet`, `require_observer`)
- UI: `postbox/web/app.js` (Fleet panel) · MCP: `postbox/mcp_server.py`
- Config knobs: `postbox/config.py` (env-overridable)
- Run: `python -m postbox.main` · Proof: `python -m scripts.fleet_e2e`
- Tests: `PYTHONPATH=<worktree> .venv/bin/python -m pytest -q`
  (the venv has an editable install pointing at `main`; PYTHONPATH overrides it)

## Open decisions
- Whether to make `POSTBOX_OBSERVER_TOKEN` required-by-default for the VM (currently
  opt-in). See backlog.
