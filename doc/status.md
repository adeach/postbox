# Status — Postbox federation build

## Goal — DONE (v1)
`agent@instance` federation per `docs/superpowers/specs/2026-07-06-postbox-federation-design.md`.
Relay-failure = soft-success. Direct 1:1 peering.

## Branch / worktree
`feat/federation` in `.worktrees/federation` (NOT main; NOT pushed).

## Shipped (gpt-5.5 subagents wrote each stage; reviewed + committed per stage)
1. `~/.postbox/config.yaml` loader (instance + peers_seed; env>yaml>default, null-safe) + pyyaml.
2. `peers` table + `PeerService` + `/peers` admin API (observer-guarded, token redacted, no-clobber seed).
3. `parse_address` + `AgentService.ensure_remote` stub rows (idempotent, offline).
4. Send-routing to peers (store-to-stub + injectable relay, soft-success) + `POST /federation/inbound`
   (peer-token auth, anti-spoof from-domain, idempotent, thread_id propagation) + `_store` core.
5. UI first-contact (`Message name@peer` for known peers) + subtle remote badge.
6. `scripts/federation_e2e.py` (two real peered servers, both-way relay, shared thread, idempotent) + docs.

## Verify
- Unit: `cd .worktrees/federation && ../../.venv/bin/python -m pytest -q`  → 132 passing.
- Live: `../../.venv/bin/python -m scripts.federation_e2e`  → PASS (exit 0).

## Next / open
- NOT pushed / no PR. Merge to main on the maintainer's go (like the earlier fleet consolidation).
- Phase-2 follow-ups in `doc/backlog.md` (read-receipt relay, store-and-forward queue, directory sync, presence relay).
