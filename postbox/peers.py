from postbox.auth import now_iso
from postbox.db import Database


class PeerService:
    def __init__(self, db: Database):
        self.db = db

    async def upsert(self, name: str, url: str, token: str) -> None:
        await self.db.execute(
            "INSERT INTO peers(name,url,token,created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET url=excluded.url, token=excluded.token",
            (name, url, token, now_iso()))

    async def remove(self, name: str) -> None:
        await self.db.execute("DELETE FROM peers WHERE name=?", (name,))

    async def get(self, name: str) -> dict | None:
        row = await self.db.fetchone(
            "SELECT name,url,token FROM peers WHERE name=?", (name,))
        if row is None:
            return None
        return dict(zip(("name", "url", "token"), row))

    async def list_peers(self) -> list[dict]:
        rows = await self.db.fetchall(
            "SELECT name,url,token FROM peers ORDER BY name")
        return [dict(zip(("name", "url", "token"), r)) for r in rows]

    async def seed(self, peers_seed: tuple[dict, ...]) -> None:
        for peer in peers_seed:
            await self.db.execute(
                "INSERT OR IGNORE INTO peers(name,url,token,created_at) VALUES (?,?,?,?)",
                (peer["name"], peer["url"], peer["token"], now_iso()))
