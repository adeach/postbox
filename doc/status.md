# Status — Postbox

## Current goal
Rebuild the web Observatory into a **human-first, Slack-style DM UI** (approved
mock: `mockups/12-slack-dm.html`). Frontend only — the backend already supports it.

Model: you are ONE person (a human identity). Sidebar = your DMs + user search +
Fleet. Impersonation is a deliberate top-right "Viewing as" control (shows that
agent's own conversations). No All-activity, no channels/`#`.

## Branch / worktree
- Branch `feat/fleet-mode`, worktree `.worktrees/fleet-mode` (all work here; not on `main`).

## Done
- Fleet mode (backend + UI + tests, 100 passing). See git log.
- UX redesign discussed (3-agent panel), iterated to an approved interactive mock:
  `mockups/12-slack-dm.html` (search→DM, Slack DM rows, impersonate dropdown, Fleet).

## Backend contract (no changes needed — reuse as-is)
- `GET /observer/agents` → [{id,name,address,profile,status}] (live presence).
- `GET /observer/threads?address=<viewer>` → [{thread_id,subject,members,last,message_count,unread{addr:n}}].
- `GET /observer/threads/{id}` → {thread_id,subject,members,messages[{id,from,to[],body,created_at,read_by[]}]}.
- `POST /observer/send {from,to,body,subject?,in_reply_to?}` → creates thread on first send.
- `POST /observer/read {as,thread_id}` → HUMAN-only (guarded). Impersonating an agent must NOT mark read.
- `POST /observer/identity {name}` → creates a HUMAN identity (profile.human=true) = "you".
- `/fleet` CRUD + `/fleet/{addr}/{run|kill|enable|disable}`; SSE `/observer/events`.

## Plan (stage by stage, commit each) — see todos table
1. markup+styles (index.html, styles.css)  2. core: you + DMs + send + live SSE
3. search→DM  4. impersonation dropdown  5. Fleet panel  6. live e2e + pytest + docs

## Run / verify
- Server (worktree): `cd .worktrees/fleet-mode && ../../.venv/bin/python -m postbox.main` (serves /ui/ from THIS tree; cwd selects the worktree).
- UI: http://127.0.0.1:8765/ui/   ·   Tests: `PYTHONPATH=$(pwd) ../../.venv/bin/python -m pytest -q`

## Open decisions
- None blocking. "You" onboarding = prompt name → POST /observer/identity, persisted in localStorage.
