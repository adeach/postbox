import pytest
from postbox.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.connect()
    yield d
    await d.close()


async def test_wal_enabled(db):
    row = await db.fetchone("PRAGMA journal_mode;")
    assert row[0].lower() == "wal"


async def test_schema_has_tables(db):
    rows = await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
    )
    names = {r[0] for r in rows}
    assert {"agents", "messages", "recipients", "attachments", "events"} <= names


async def test_execute_and_fetch(db):
    await db.execute(
        "INSERT INTO agents(id,name,address,token_hash,created_at) VALUES (?,?,?,?,?)",
        ("a1", "A", "a", "h", "2026-01-01T00:00:00Z"),
    )
    row = await db.fetchone("SELECT name FROM agents WHERE id=?", ("a1",))
    assert row[0] == "A"


async def test_agents_has_v2_columns(db):
    rows = await db.fetchall("PRAGMA table_info(agents);")
    cols = {r[1] for r in rows}
    assert {"wakeup_kind", "wakeup_target", "status", "last_seen"} <= cols
