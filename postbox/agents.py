import json

from postbox.auth import generate_token, hash_token, new_id, now_iso
from postbox.db import Database
from postbox.models import AgentOut, RegisterAgent, RegisterResult


class AgentService:
    def __init__(self, db: Database):
        self.db = db

    async def register(self, payload: RegisterAgent) -> RegisterResult:
        # Reattach: a resumed Copilot session carries the same session_key, so it
        # rebinds to its existing identity (same inbox/threads) instead of creating a
        # new one. Rotate the token, flip back online (un-hides a 'forgotten' row too).
        if payload.session_key:
            row = await self.db.fetchone(
                "SELECT id,name,address,profile FROM agents WHERE session_key=?",
                (payload.session_key,))
            if row:
                token = generate_token()
                await self.db.execute(
                    "UPDATE agents SET token_hash=?, status='online', last_seen=? "
                    "WHERE id=?", (hash_token(token), now_iso(), row[0]))
                return RegisterResult(
                    id=row[0], name=row[1], address=row[2],
                    profile=json.loads(row[3]) if row[3] else None, token=token)

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
            "wakeup_kind,wakeup_target,status,last_seen,session_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (agent_id, name, address,
             json.dumps(payload.profile) if payload.profile else None,
             hash_token(token), now,
             payload.wakeup.kind, payload.wakeup.target, "online", now,
             payload.session_key),
        )
        return RegisterResult(id=agent_id, name=name, address=address,
                              profile=payload.profile, token=token)

    async def ensure_remote(self, address: str, peer: str) -> str:
        existing = await self.db.fetchone(
            "SELECT id FROM agents WHERE address=?", (address,))
        if existing:
            return existing[0]

        agent_id = new_id()
        now = now_iso()
        await self.db.execute(
            "INSERT INTO agents(id,name,address,profile,token_hash,created_at,"
            "wakeup_kind,wakeup_target,status,last_seen) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (agent_id, address, address,
             json.dumps({"remote": True, "peer": peer}),
             hash_token(generate_token()), now,
             "none", None, "offline", now),
        )
        return agent_id

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
        # 'deregistered' = the identity is GONE (session stopped / left), distinct from
        # merely 'offline' (away, may reconnect). The directory excludes the former.
        await self.db.execute(
            "UPDATE agents SET status='deregistered' WHERE id=?", (agent_id,))

    async def _get(self, agent_id: str) -> AgentOut:
        r = await self.db.fetchone(
            "SELECT id,name,address,profile,status FROM agents WHERE id=?", (agent_id,))
        return AgentOut(id=r[0], name=r[1], address=r[2],
                        profile=json.loads(r[3]) if r[3] else None, status=r[4])

    async def directory(self, online_ids: set[str]) -> list[AgentOut]:
        """Recipient directory. Presence is LIVE (from EventBus.online_ids()), so the
        stored `status` latch is ignored — `status` is annotated truthfully from whether
        the identity currently holds an SSE connection. All registered identities are
        listed (you can message an offline peer; it queues), each honestly labelled
        online/offline. Deregistered (gone) identities are excluded; reaping otherwise-
        dead ephemeral sessions is a separate concern."""
        rows = await self.db.fetchall(
            "SELECT id,name,address,profile FROM agents "
            "WHERE status<>'deregistered' ORDER BY address")
        return [AgentOut(id=r[0], name=r[1], address=r[2],
                         profile=json.loads(r[3]) if r[3] else None,
                         status="online" if r[0] in online_ids else "offline")
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

    async def get_by_id(self, agent_id: str) -> AgentOut | None:
        row = await self.db.fetchone(
            "SELECT id,name,address,profile,status FROM agents WHERE id=?", (agent_id,))
        if not row:
            return None
        return AgentOut(id=row[0], name=row[1], address=row[2],
                        profile=json.loads(row[3]) if row[3] else None, status=row[4])
