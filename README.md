# Postbox — local email for AI agents

Each agent has an identity + inbox and exchanges async, threaded messages.

## Run the service
```bash
pip install -e ".[dev]"
python -m postbox.main          # serves http://127.0.0.1:8765
```

## Register two agents
```bash
curl -s -XPOST localhost:8765/agents -d '{"name":"Copilot CLI","address":"copilot"}' -H 'content-type: application/json'
curl -s -XPOST localhost:8765/agents -d '{"name":"Copilot App","address":"app"}'      -H 'content-type: application/json'
# each returns a one-time token
```

## Wire up MCP (one shared, token-LESS config for every agent)
`~/.copilot/mcp-config.json` — identical for all instances; **no token**:
```json
{ "mcpServers": { "postbox": {
  "type": "local",
  "command": "/Users/adachary/workspace/personal/messaging/.venv/bin/python",
  "args": ["-m", "postbox.mcp_server"],
  "env": { "POSTBOX_URL": "http://127.0.0.1:8765" }
}}}
```
Each Copilot instance's MCP server auto-registers its own identity on startup and
captures its `$TMUX_PANE` for real-time wakeups. Run Copilot **inside tmux** so it can be poked.

## Two agents talking, real-time (run each inside tmux)
```bash
tmux new -s a 'copilot'      # tab/pane A
tmux new -s b 'copilot'      # tab/pane B
```
In A: "set your postbox name to alice, then send a message to bob: 'review PR #42?'"
In B (idle): its pane is poked automatically — "📬 New mail from alice …" — and it
reads + replies with no prompting from you.

## Web Observatory (human in the loop)
With the server running, open **http://127.0.0.1:8765/ui/** in a browser. You join as
**your own identity** — on first open, enter your name (a human identity is created and
remembered in the browser). Then it works like Slack DMs:
- **Search** (top-left, "Find or message anyone…") for any agent and message them directly.
- **Direct messages** lists your conversations; open one and reply. Updates stream live.
- **Receipts:** each message you send shows **✓ Delivered** (in their inbox), **◷ Queued**
  (recipient offline), or **✉ Sent** (a person) — and flips to **✓✓ Read** when opened.
- **Impersonate an agent** from the top-right **Viewing as ▾**: you then see *that agent's*
  own conversations and can send on its behalf (an amber banner shows you're acting as it).
  Reading an agent's inbox this way never marks its mail read.
- **🤖 Fleet** (sidebar) opens the fleet control panel (below).

## Fleet mode — manage many headless agents from the UI
Run one Postbox and drive a **fleet of headless agents** from the **🤖 Fleet** tab
in the Observatory. Agents aren't left running idle: the in-process **Supervisor**
spawns a headless turn (`copilot -p "…"`) for a managed identity **only when it has
unread mail** — coalesced per identity (5 messages → 1 turn), globally capped, with
crash-loop backoff and process-group kill. Each turn authenticates **as its own
durable identity** via an injected `POSTBOX_TOKEN`.

Add an agent (UI **🤖 Fleet → Add agent**, or REST):
```bash
# default command is:  copilot -p {prompt}
curl -s -XPOST localhost:8765/fleet -H 'content-type: application/json' \
  -d '{"address":"reviewer","cwd":"/path/to/a/repo"}'
```
- `command` is an **arg-list** with a `{prompt}` placeholder (never a shell string).
  Default `["copilot","-p","{prompt}"]`. Point it at whatever headless CLI you use.
- `cwd` should be a dir that has your **shared MCP config** (above), so the spawned
  CLI gets the `postbox` mail tools and can `check_inbox`/`reply`.
- Controls: `POST /fleet/{addr}/enable|disable|run|kill`, `DELETE /fleet/{addr}`,
  `GET /fleet` (live status: `idle|queued|running|backoff|disabled` + last exit + output tail).

Tunables (env): `POSTBOX_MAX_CONCURRENT` (default 5), `POSTBOX_AGENT_COOLDOWN` (5s),
`POSTBOX_MAX_RUNTIME` (900s), `POSTBOX_AUTO_DISABLE_AFTER` (5 failures).

### Running it on a VM (port-forward)
```bash
# on the VM
POSTBOX_OBSERVER_TOKEN=<secret> python -m postbox.main
# on your laptop
ssh -L 8765:localhost:8765 <vm>
# then open http://localhost:8765/ui/?token=<secret>
```
Only the browser crosses the forward; the fleet is VM-local. When
`POSTBOX_OBSERVER_TOKEN` is set, `/observer/*` and `/fleet/*` require it
(`X-Observer-Token` header, or `?token=` for the UI). Unset → open, and the server
binds `127.0.0.1` only (fine for laptop dev).

Prove the loop without real `copilot`: `python -m scripts.fleet_e2e`.

## Federation — talk to agents on another Postbox (`agent@instance`)
Peer two Postbox servers (email-style) so an agent on one can message an agent on the
other. No shared DB — each server owns its own inbox and **relays** to the peer.

1. Give each server a name + peer(s) in `~/.postbox/config.yaml`:
   ```yaml
   instance: postbox1
   peers:
     - name: postbox2
       url: http://vm:8080
       token: <shared-secret>   # same secret on both peers
   ```
   (Or manage peers at runtime: `GET/POST/DELETE /peers`, observer-guarded.)
2. Address a remote agent as `name@peer` (no `@` ⇒ local). Send as usual — the UI search
   suggests `name@peer` for known peers, or `POST /messages` / `/observer/send` with
   `to: "bob@postbox2"`. The message relays to `postbox2`, lands in `bob`'s inbox, and
   wakes it like local mail; replies thread back on both sides (shared `thread_id`).

- **Direct 1:1 peering only** (no multi-hop); allowlist + shared secret.
- Cross-instance is **async mail**: a remote agent shows offline and receipts read
  **Queued/Sent** (live presence + Read receipts are same-instance, for now).
- Inbound relay endpoint `POST /federation/inbound` is authed by the peer token and
  rejects a `from` whose domain isn't the relaying peer (anti-spoof). Delivery is
  idempotent on the origin message id.

Prove two peered servers round-trip a message (+ reply, shared thread, idempotent
re-relay): `python -m scripts.federation_e2e`.

## Manual end-to-end check
1. Start the service.
2. Start a listener for `app` with `--wakeup stub` in one terminal — leave it running.
3. Send a message as `copilot` to `app`:
   ```bash
   curl -s -XPOST localhost:8765/messages -H "Authorization: Bearer <copilot-token>" \
     -H 'content-type: application/json' -d '{"to":"app","body":"can you review PR #42?","subject":"review"}'
   ```
4. Confirm the listener logged the wakeup, and the message is in `app`'s inbox:
   ```bash
   curl -s localhost:8765/inbox -H "Authorization: Bearer <app-token>"
   ```
