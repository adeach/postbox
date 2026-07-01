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
- Tests: `tests/test_fleet.py` (10, incl. real group-kill), `tests/test_fleet_api.py`
  (3). Full suite **91 passing**.
- Live proof: `scripts/fleet_e2e.py` — real subprocess authenticates via injected
  token, coalescing verified. Passes.
- README "Fleet mode" section (add agents, VM port-forward, tunables).

## Next step
- Awaiting the `fleet-code-review` findings (in flight); fold in any real bugs.
- Then: user tests on laptop (swap command to real `copilot -p {prompt}`), then VM.
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
