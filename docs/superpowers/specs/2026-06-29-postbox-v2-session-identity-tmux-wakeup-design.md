# Postbox v2 — Session Identity + Real-Time tmux Wakeup — Design Spec

**Date:** 2026-06-29
**Status:** Approved approach (dialogue) — spec for review
**Builds on:** v1 (`2026-06-29-agent-messaging-platform-design.md`) — REST+SSE service, durable inbox, MCP server, listener daemon. v2 keeps all of it and changes how **identity** and **delivery** work.

## 1. Why v2 (the three problems with v1)

1. **Identity is wired by hand.** v1 puts a token in `mcp-config.json`, so an instance's identity is something you configure. It should just *exist* when you open the agent.
2. **There is only one `mcp-config.json`.** All Copilot instances share it, so a config-baked token can only ever yield one identity. Identity must not come from the static config.
3. **Receipt is manual / not real-time.** v1 needs the agent to be *asked* to check its inbox, and can't interrupt an idle agent. The whole point is: **copilot1 sends → copilot2 receives it in real time**, with no manual checking, whether copilot2 is idle or mid-task.

## 2. Core ideas

**A. Identity is per-session and self-assigned — not configured.**
Each Copilot instance launches its *own* `postbox.mcp_server` subprocess (stdio servers are per-client-process). So the MCP server **auto-registers an identity on startup** and holds the token in memory for that session. The shared config carries **no token** — only `COURIER_URL`. Identical for every instance.
- `id` = a **session id** (generated per launch; adopt a Copilot-provided session id if one is exposed via env).
- `name` = **self-assigned by the agent** (a `set_name` tool), defaulting to `copilot-<short-id>` until set. Peers address each other by name.

**B. Delivery is real-time via terminal injection (tmux).**
You can't push into Copilot through MCP (pull-only), but you can type into the **tmux pane** Copilot runs in. The MCP server, launched inside a pane, inherits `$TMUX_PANE`; it registers that as its **wakeup target**. When mail arrives, a watcher does:
```
tmux send-keys -l -t <pane> "📬 New mail from <sender> — use your postbox tools to read and reply."
tmux send-keys    -t <pane> Enter
```
To an idle agent this lands as if typed → it wakes and acts. To a busy agent it buffers and is consumed at the next prompt. Real-time (SSE latency + send-keys = ms). Nothing is lost: the durable inbox + an auto-check instruction are the backstop.

## 3. Architecture — collapse the watcher into the MCP server

The MCP server is already per-session and already knows the token (it registered) and the pane (`$TMUX_PANE`). So it also **runs the SSE wakeup loop itself** as a background task and pokes its **own** pane. No separate daemon to launch.

```
Copilot session (in tmux pane P)
  └─ spawns postbox.mcp_server  (one shared, token-less config)
        startup:  POST /agents {name?, wakeup:{type:tmux, pane:P}} -> token (held in memory)
        provides: MCP tools (list_agents, send_message, check_inbox, read_message, reply, set_name)
        background task: GET /events (SSE) ─ on message.received ─► tmux send-keys into pane P
        shutdown: DELETE /agents/self  (presence: drop from directory)

copilot1.send_message(to="bob") ─► Postbox commits to inbox, emits event
                                         │ SSE (real-time)
                                         ▼
                                bob's MCP server background task ─ tmux send-keys ─► bob's pane wakes
```

**Why collapse vs. a separate relay:** zero extra processes, the watcher inherently has the right token+pane, lifecycle matches the session exactly. (Alternative — a single central relay holding all tokens/panes — is noted in §8 but not chosen: it needs a way to learn every agent's token, which the per-session server already has for free.)

## 4. Changes from v1

### Data model (`agents`)
Add:
- `wakeup_kind` TEXT — `'tmux' | 'os_notify' | 'none'`
- `wakeup_target` TEXT — e.g. the `$TMUX_PANE` value (nullable)
- `last_seen` TEXT — for presence/liveness
- `status` TEXT — `'online' | 'offline'` (derived from register/deregister/heartbeat)

`id` becomes the session id. `name` is mutable. Address resolution is by `name` among **online** agents.

### API
- `POST /agents` — now also accepts `{wakeup:{kind,target}}`; returns id+token. Identity is ephemeral (session-scoped).
- `PATCH /agents/self` — set/update `name` (powers `set_name`); rejects a name already taken by another online agent.
- `DELETE /agents/self` — deregister on shutdown (presence).
- `GET /agents` — directory now returns **online** agents with `name` + capability/profile (presence view).
- Heartbeat: either a periodic `PATCH /agents/self/ping` from the MCP server, or treat the live SSE connection as the liveness signal (preferred — the background SSE task *is* the heartbeat; on disconnect, mark offline).
- Everything else (send/inbox/read/thread/events) unchanged.

### MCP server (`postbox.mcp_server`)
- On startup: auto-register (read `$TMUX_PANE`; if unset, `wakeup_kind='none'` + log a warning), store token in memory.
- Add tool `set_name(name)`.
- Start a background asyncio task running the SSE loop; on `message.received`, run the tmux send-keys wakeup (reusing v1's wakeup strategies, now driven in-process).
- Provide MCP **server instructions** (Copilot includes them via `--allow-all-mcp-server-instructions`): *"You have a postbox mailbox. When you receive a '📬 New mail' line, call check_inbox then read_message and act. Also check_inbox at the start of a turn if unsure."* — belt-and-suspenders with the poke.
- On shutdown: best-effort `DELETE /agents/self`.

### New wakeup strategy
- `TmuxWakeup(pane)` → `tmux send-keys -l -t <pane> <text>` then `tmux send-keys -t <pane> Enter`. Text passed **literally** (`-l`) to avoid interpreting special chars; Enter sent separately. Escapes/edge cases (no pane, dead pane) handled gracefully (log, fall back to `none`).

### Listener daemon (v1)
Becomes optional/legacy — the MCP server self-watches now. Keep it for non-tmux or headless setups (`copilot -p`, `ghapp://`). Not the primary path in v2.

## 5. Wakeup content & loop-safety
- The injected line is a short **instruction**, never the full body (the agent fetches via MCP) — keeps the pane clean and avoids leaking large content.
- Events are emitted only to **recipients**, so an agent is never poked for its own sent mail. No echo loop.
- Debounce: if multiple messages arrive in a burst, coalesce into one poke ("📬 N new messages") to avoid spamming the pane.

## 6. Security / scope assumptions (local v1-class)
- Single machine, all agents in tmux, trusted local user.
- Terminal injection = anything that can reach your tmux can drive an agent. Acceptable locally; explicitly **not** a multi-tenant/remote security model.
- Tokens still hashed at rest; an agent still can't read another's inbox/events.

## 7. Scope

### v2 build (vertical slice)
- Session auto-registration in the MCP server (token-less shared config), `$TMUX_PANE` capture, in-memory token.
- `set_name` tool + name-based addressing among online agents; presence via the SSE connection as liveness.
- `TmuxWakeup` + MCP server background SSE loop poking its own pane.
- MCP server instructions for auto-check.
- **Demo:** two Copilot tabs in two tmux panes, token-less shared config; copilot1 `send_message(to=<name>)` → copilot2's pane is **poked in real time** and the agent reads + replies — with no human "check your inbox," idle or busy.

### Roadmap (later)
- Heartbeat/TTL reaping of stale sessions; reconnect/resume of identity.
- Stable cross-session handles (claim a durable name).
- Central relay option; non-tmux surfaces (app `ghapp://`, headless spawn).
- Burst debounce tuning; configurable poke templates.
- Carry over v1.x follow-ups (idempotency TOCTOU, N+1).

## 8. Open design decisions (flag for review)
1. **Collapse vs. separate relay** — spec recommends collapsing the watcher into the MCP server (simplest, right token+pane for free). Alternative: one central relay. → Recommend **collapse**.
2. **Session id source** — generate a uuid at launch vs. adopt a Copilot-provided session id if present in env. → Recommend **generate**, adopt env id if/when verified.
3. **Liveness signal** — SSE connection presence vs. explicit heartbeat ping. → Recommend **SSE-as-heartbeat** (one mechanism).
4. **Addressing** — by `name` among online agents (recommended) vs. also expose session id. Name collisions rejected on `set_name`.
5. **Backstop** — keep v1 durable inbox + auto-check instruction even though tmux poke is primary (recommended: yes, belt-and-suspenders).

## 9. Testing strategy
- **Auto-register:** launching the MCP server registers an identity with the `$TMUX_PANE` it inherited; unset pane → `wakeup_kind='none'` + warning.
- **set_name:** updates name; duplicate online name rejected; peers resolve by name.
- **TmuxWakeup:** with a fake runner, asserts `send-keys -l <text>` + `Enter` to the right pane; real tmux integration test in a throwaway tmux session asserting the text actually lands in a pane (capture-pane).
- **End-to-end (real tmux):** spawn two panes each running a stub "agent" (a shell reading lines); register both via real MCP servers; send mail; assert (via `tmux capture-pane`) the recipient pane receives the poke line within a 2s timeout, and the message is in its inbox.
- **Presence:** SSE disconnect marks the agent offline / drops from directory.
- Reuse v1's suite unchanged.

## 10. Stack
Unchanged from v1 (Python/FastAPI/SQLite/aiosqlite/MCP SDK/httpx). New dependency on the `tmux` binary at runtime for the wakeup path (graceful fallback when absent).
