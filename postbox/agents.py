import json

from postbox.auth import generate_token, hash_token, new_id, now_iso
from postbox.db import Database
from postbox.models import AgentOut, RegisterAgent, RegisterResult


class AgentService:
    def __init__(self, db: Database):
        self.db = db

    async def register(self, payload: RegisterAgent) -> RegisterResult:
        agent_id = new_id()
        name = payload.name or f"copilot-{agent_id[:8]}"
        address = payload.address or name
        existing = await self.db.fetchone(
            "SELECT id FROM agents WHERE address=?", (address,))
        if existing:
            raise ValueError(f"address already registered: {address}")
        token = generate_token()
        now = now_iso()
        await self.db.execute(
            "INSERT INTO agents(id,name,address,profile,token_hash,created_at,"
            "wakeup_kind,wakeup_target,status,last_seen) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (agent_id, name, address,
             json.dumps(payload.profile) if payload.profile else None,
             hash_token(token), now,
             payload.wakeup.kind, payload.wakeup.target, "online", now),
        )
        return RegisterResult(id=agent_id, name=name, address=address,
                              profile=payload.profile, token=token)

    async def set_name(self, agent_id: str, name: str) -> AgentOut:
        taken = await self.db.fetchone(
            "SELECT id FROM agents WHERE address=? AND id<>?", (name, agent_id))
        if taken:
            raise ValueError(f"name already taken: {name}")
        await self.db.execute(
            "UPDATE agents SET name=?, address=? WHERE id=?", (name, name, agent_id))
        return await self._get(agent_id)

    async def set_status(self, agent_id: str, status: str) -> None:
        await self.db.execute(
            "UPDATE agents SET status=?, last_seen=? WHERE id=?",
            (status, now_iso(), agent_id))

    async def deregister(self, agent_id: str) -> None:
        await self.db.execute("UPDATE agents SET status='offline' WHERE id=?", (agent_id,))

    async def _get(self, agent_id: str) -> AgentOut:
        r = await self.db.fetchone(
            "SELECT id,name,address,profile,status FROM agents WHERE id=?", (agent_id,))
        return AgentOut(id=r[0], name=r[1], address=r[2],
                        profile=json.loads(r[3]) if r[3] else None, status=r[4])

    async def directory(self, include_offline: bool = False) -> list[AgentOut]:
        sql = "SELECT id,name,address,profile,status FROM agents"
        if not include_offline:
            sql += " WHERE status='online'"
        sql += " ORDER BY address"
        rows = await self.db.fetchall(sql)
        return [AgentOut(id=r[0], name=r[1], address=r[2],
                         profile=json.loads(r[3]) if r[3] else None, status=r[4])
                for r in rows]

    async def resolve_token(self, token: str) -> AgentOut | None:
        row = await self.db.fetchone(
            "SELECT id,name,address,profile,status FROM agents WHERE token_hash=?",
            (hash_token(token),))
        if not row:
            return None
        return AgentOut(id=row[0], name=row[1], address=row[2],
                        profile=json.loads(row[3]) if row[3] else None, status=row[4])

    async def get_by_address(self, address: str) -> AgentOut | None:
        row = await self.db.fetchone(
            "SELECT id,name,address,profile,status FROM agents WHERE address=?", (address,))
        if not row:
            return None
        return AgentOut(id=row[0], name=row[1], address=row[2],
                        profile=json.loads(row[3]) if row[3] else None, status=row[4])
