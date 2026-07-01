# Backlog

Follow-up work surfaced while building Fleet mode. Not doing now — logged so it
isn't lost.

- [ ] Fleet fairness — round-robin across identities under the cap; one busy agent
      can dominate requeues — status: later (ponytail: FIFO/dedupe is fine for dozens)
- [ ] Per-agent concurrency > 1 (a single identity handling turns in parallel) —
      status: later (coalescing to 1 is intended for now)
- [ ] Observer token: consider required-by-default (bind + token) for VM deploys
      instead of opt-in; today it's opt-in so laptop dev is frictionless —
      status: todo (decision needed)
- [ ] Reap/replace an offline identity's name (H2, still open) — fleet mode
      *sidesteps* it via stored token, but truly reclaiming a name needs the reaping
      tier — status: later
- [ ] Spawn/turn metrics (count, durations) exposed on `/fleet` for observability —
      status: later
- [ ] `run` on a disabled agent currently spawns a one-off turn (ignores enabled) —
      confirm that's the desired "force" semantics or gate it — status: todo
- [ ] Launch/infra failures call `record_exit(127)`, so a transient DB blip counts
      toward `auto_disable_after` (a bad *binary* auto-disabling is correct; a DB
      hiccup being attributed to the agent is debatable). Consider only counting real
      child exits toward crash-loop backoff — status: later (harmless to correctness)

## Pre-existing audit items (not touched by Fleet mode)
See `docs/superpowers/specs/2026-06-30-postbox-correctness-audit.md`.
- [ ] C1 DB read/write isolation (single aiosqlite conn) — status: later
- [ ] C6 stored XSS in the UI + observer-auth — auth now opt-in via fleet work;
      XSS still open — status: todo
- [ ] C5 group-reply misroute — status: later
- [ ] H5 atomic message+event; H6 bounded firehose replay — status: later
