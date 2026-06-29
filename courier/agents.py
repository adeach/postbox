import json

from courier.auth import generate_token, hash_token, new_id, now_iso
from courier.db import Database
from courier.models import AgentOut, RegisterAgent, RegisterResult


class AgentService:
    def __init__(self, db: Database):
        self.db = db

    async def register(self, payload: RegisterAgent) -> RegisterResult:
        existing = await self.db.fetchone(
            "SELECT id FROM agents WHERE address=?", (payload.address,)
        )
        if existing:
            raise ValueError(f"address already registered: {payload.address}")

        token = generate_token()
        agent_id = new_id()
        await self.db.execute(
            "INSERT INTO agents(id,name,address,profile,token_hash,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                agent_id,
                payload.name,
                payload.address,
                json.dumps(payload.profile) if payload.profile else None,
                hash_token(token),
                now_iso(),
            ),
        )
        return RegisterResult(
            id=agent_id, name=payload.name, address=payload.address,
            profile=payload.profile, token=token,
        )

    async def directory(self) -> list[AgentOut]:
        rows = await self.db.fetchall(
            "SELECT id,name,address,profile FROM agents ORDER BY address"
        )
        return [
            AgentOut(
                id=r[0], name=r[1], address=r[2],
                profile=json.loads(r[3]) if r[3] else None,
            )
            for r in rows
        ]

    async def resolve_token(self, token: str) -> AgentOut | None:
        row = await self.db.fetchone(
            "SELECT id,name,address,profile FROM agents WHERE token_hash=?",
            (hash_token(token),),
        )
        if not row:
            return None
        return AgentOut(
            id=row[0], name=row[1], address=row[2],
            profile=json.loads(row[3]) if row[3] else None,
        )

    async def get_by_address(self, address: str) -> AgentOut | None:
        row = await self.db.fetchone(
            "SELECT id,name,address,profile FROM agents WHERE address=?", (address,)
        )
        if not row:
            return None
        return AgentOut(
            id=row[0], name=row[1], address=row[2],
            profile=json.loads(row[3]) if row[3] else None,
        )
