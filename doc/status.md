# Status — Postbox federation build

## Goal
Implement `agent@instance` federation per `docs/superpowers/specs/2026-07-06-postbox-federation-design.md`.
Relay-failure UX = soft-success (confirmed). Direct 1:1 peering only.

## Branch / worktree
`feat/federation` in `.worktrees/federation` (NOT main; NOT pushed).

## Method
gpt-5.5 subagents write each stage; Opus 4.8 (leader) reviews the actual files vs spec,
corrects, runs tests, commits per stage. Todos: fed-1..fed-6.

## Run / test (IMPORTANT: cwd must be the worktree so imports resolve to it)
`cd .worktrees/federation && ../../.venv/bin/python -m pytest -q`   (baseline: 100 passing)

## Stages
1 config.yaml · 2 peers table + /peers · 3 address parse + stub agents ·
4 send routing + /federation/inbound · 5 UI first-contact · 6 multi-instance live e2e + docs

## Acceptance gate
Multi-instance live e2e: two real peered servers (separate data dirs/ports/instance names),
agent1@postbox1 -> agent2@postbox2 delivers + wakes + reply + shared thread + idempotent re-relay.
