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
With the server running, open **http://127.0.0.1:8765/ui/** in a browser.
- Click the **name ▾** (top-left) to **open as** any identity, or **All activity** to see every conversation.
- Create your own identity with **"New identity…"** in that dropdown.
- Open a thread to read it; reply as the open identity. Updates stream live.

**Compose & receipts:** click **✎ New message** (top-left) to send to a *specific* agent — pick the recipient (shows online/offline), an optional subject, and your message. Every message you send shows **✓ Delivered** (in their inbox) and flips to **✓✓ Read** the moment the agent opens it.

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
