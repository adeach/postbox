# Postbox — status

## Current
All agent-management work MERGED to `main` (HEAD 7e08fcf), 162 tests pass.
Laptop server runs from `main` on :8765 (real ~/.postbox data + federation peer `vm`).
Mutagen syncs `main` → VM `~/mutagen/messaging` (in sync).

## Shipped this session (on main)
- Resumable identities: session_key (COPILOT_AGENT_SESSION_ID) reattach; persist on exit.
- UI Forget (soft) + session-id chip; 💬 Chat button on agent/terminal rows.
- Terminal agents: spin up interactive copilot in tmux (postbox_<name>), --allow-all-tools + --allow-all-mcp-server-instructions (autonomous, no prompts, gets postbox collab instructions), UI + /terminals + Bearer /spawn.
- spawn_terminal MCP tool (agent spawns + chats another copilot), waits for registration.
- All-conversations read-only watch view.
- Cross-instance remote spawn: spawn_terminal(instance=peer) -> /federation/spawn (peer-token), addressable name@peer. terminal_cmd + spawn_wait config.

## Next: real cross-VM test (needs VM postbox restart)
1. On VM: restart postbox from ~/mutagen/messaging (picks up synced new code). config.yaml (~/.postbox) unchanged.
2. Verify laptop->VM /federation/spawn relay (curl or a fresh copilot's spawn_terminal(instance=vm)).
3. Chat helper@vm.

## Backlog
- doc/backlog.md — federation phase-2 (receipts/presence relay), auth gap, etc.
