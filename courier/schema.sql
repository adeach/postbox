CREATE TABLE IF NOT EXISTS agents (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  address     TEXT UNIQUE NOT NULL,
  profile     TEXT,
  token_hash  TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id              TEXT PRIMARY KEY,
  thread_id       TEXT NOT NULL,
  in_reply_to     TEXT,
  sender_id       TEXT NOT NULL REFERENCES agents(id),
  subject         TEXT,
  body            TEXT NOT NULL,
  content_type    TEXT NOT NULL DEFAULT 'text/plain',
  idempotency_key TEXT,
  created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_idem
  ON messages(sender_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_messages_thread ON messages(thread_id);

CREATE TABLE IF NOT EXISTS recipients (
  message_id   TEXT NOT NULL REFERENCES messages(id),
  agent_id     TEXT NOT NULL REFERENCES agents(id),
  kind         TEXT NOT NULL,
  delivered_at TEXT,
  read_at      TEXT,
  PRIMARY KEY (message_id, agent_id)
);
CREATE INDEX IF NOT EXISTS ix_recipients_agent ON recipients(agent_id);

CREATE TABLE IF NOT EXISTS attachments (
  id           TEXT PRIMARY KEY,
  message_id   TEXT NOT NULL REFERENCES messages(id),
  filename     TEXT NOT NULL,
  content_type TEXT NOT NULL,
  size         INTEGER NOT NULL,
  blob_path    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id   TEXT NOT NULL REFERENCES agents(id),
  type       TEXT NOT NULL,
  payload    TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_agent_id ON events(agent_id, id);
