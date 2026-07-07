import asyncio
from pathlib import Path

import aiosqlite

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()


class Database:
    """Single shared aiosqlite connection. All ops run on one background thread,
    so they are inherently serialized; a write lock guards multi-statement writes."""

    def __init__(self, path: Path):
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        """Additive, idempotent migrations for existing databases."""
        cur = await self._conn.execute("PRAGMA table_info(agents);")
        cols = {r[1] for r in await cur.fetchall()}
        adds = {
            "wakeup_kind": "TEXT NOT NULL DEFAULT 'none'",
            "wakeup_target": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'online'",
            "last_seen": "TEXT",
            "session_key": "TEXT",
        }
        for col, decl in adds.items():
            if col not in cols:
                await self._conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {decl};")
        # one Copilot session ↔ one identity (the reattach key). Created here, not in
        # schema.sql, because the column may not exist until the ALTER above runs.
        await self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_agents_session_key "
            "ON agents(session_key) WHERE session_key IS NOT NULL;")
        # A row that predates the status column has no live SSE session behind it,
        # so it must NOT appear online. The ADD COLUMN default is 'online' (correct
        # for new registrations); flip pre-existing rows to 'offline' on first upgrade.
        if "status" not in cols:
            await self._conn.execute("UPDATE agents SET status='offline';")

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not connected"
        return self._conn

    @property
    def write_lock(self) -> asyncio.Lock:
        return self._write_lock

    async def execute(self, sql: str, params: tuple = ()) -> None:
        async with self._write_lock:
            try:
                await self.conn.execute(sql, params)
                await self.conn.commit()
            except Exception:
                # a failed write (e.g. a constraint violation) must not leave an open
                # transaction on the single shared connection for the next caller
                await self.conn.rollback()
                raise

    async def fetchone(self, sql: str, params: tuple = ()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchall()
