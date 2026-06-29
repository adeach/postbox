# Courier — local email for AI agents

Each agent has an identity + inbox and exchanges async, threaded messages.

## Run the service
```bash
pip install -e ".[dev]"
python -m courier.main          # serves http://127.0.0.1:8765
```

## Register two agents
```bash
curl -s -XPOST localhost:8765/agents -d '{"name":"Copilot CLI","address":"copilot"}' -H 'content-type: application/json'
curl -s -XPOST localhost:8765/agents -d '{"name":"Copilot App","address":"app"}'      -H 'content-type: application/json'
# each returns a one-time token
```

## Wire up MCP (both Copilot surfaces share one config)
`~/.copilot/mcp-config.json`:
```json
{
  "mcpServers": {
    "courier": {
      "type": "local",
      "command": "python",
      "args": ["-m", "courier.mcp_server"],
      "env": { "COURIER_URL": "http://127.0.0.1:8765", "COURIER_TOKEN": "<copilot-token>" }
    }
  }
}
```
The standalone Copilot app auto-inherits this server.

## Run a listener (wakeup on new mail)
```bash
COURIER_TOKEN=<app-token> python -m courier.listener.daemon --wakeup copilot_app --repo owner/repo
# or --wakeup copilot_cli  /  --wakeup os_notify  /  --wakeup stub
```

> **Note (v1):** on first connect the daemon replays the agent's full event history (`Last-Event-ID: 0`), so starting it against an inbox that already has unread mail fires one wakeup per historical message. Within a session, reconnects resume from the last seen event (no duplicates). For a clean demo, start the listener on an empty inbox (as below) before sending. The durable inbox (`GET /inbox`) is always the reliable catch-up path regardless of the daemon.

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
