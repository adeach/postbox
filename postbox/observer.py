import json

from postbox.agents import AgentService
from postbox.db import Database
from postbox.messages import MessageService
from postbox.models import (AgentFull, MessageView, RegisterAgent,
                            SendMessage, ThreadDetail, ThreadSummary)


class ObserverService:
    """Global, identity-agnostic reads + send-as for the web Observatory.
    Reuses AgentService/MessageService; adds unfiltered (all-agents) queries."""

    def __init__(self, db: Database, agents: AgentService, messages: MessageService):
        self.db = db
        self.agents = agents
        self.messages = messages

    async def agents_all(self) -> list[AgentFull]:
        rows = await self.db.fetchall(
            "SELECT id,name,address,profile,status FROM agents ORDER BY "
            "status='offline', address")
        return [AgentFull(id=r[0], name=r[1], address=r[2],
                          profile=json.loads(r[3]) if r[3] else None, status=r[4])
                for r in rows]

    async def _thread_ids(self, address: str | None) -> list[str]:
        if address is None:
            rows = await self.db.fetchall(
                "SELECT thread_id, MAX(created_at) mx FROM messages "
                "GROUP BY thread_id ORDER BY mx DESC")
            return [r[0] for r in rows]
        agent = await self.agents_get_id(address)
        if agent is None:
            return []
        rows = await self.db.fetchall(
            "SELECT m.thread_id, MAX(m.created_at) mx FROM messages m "
            "WHERE m.sender_id=? OR m.id IN ("
            "  SELECT message_id FROM recipients WHERE agent_id=?) "
            "GROUP BY m.thread_id ORDER BY mx DESC",
            (agent, agent))
        return [r[0] for r in rows]

    async def agents_get_id(self, address: str) -> str | None:
        row = await self.db.fetchone("SELECT id FROM agents WHERE address=?", (address,))
        return row[0] if row else None

    async def _summary(self, tid: str) -> ThreadSummary:
        subj = await self.db.fetchone("SELECT subject FROM messages WHERE id=?", (tid,))
        members = [r[0] for r in await self.db.fetchall(
            "SELECT DISTINCT a.address FROM agents a WHERE a.id IN ("
            "  SELECT sender_id FROM messages WHERE thread_id=? "
            "  UNION SELECT r.agent_id FROM recipients r JOIN messages m "
            "    ON m.id=r.message_id WHERE m.thread_id=?) ORDER BY a.address",
            (tid, tid))]
        last = await self.db.fetchone(
            "SELECT a.address, m.body, m.created_at FROM messages m "
            "JOIN agents a ON a.id=m.sender_id WHERE m.thread_id=? "
            "ORDER BY m.created_at DESC LIMIT 1", (tid,))
        count = (await self.db.fetchone(
            "SELECT COUNT(*) FROM messages WHERE thread_id=?", (tid,)))[0]
        unread = {r[0]: r[1] for r in await self.db.fetchall(
            "SELECT a.address, COUNT(*) FROM recipients r "
            "JOIN messages m ON m.id=r.message_id JOIN agents a ON a.id=r.agent_id "
            "WHERE m.thread_id=? AND r.read_at IS NULL GROUP BY a.address", (tid,))}
        return ThreadSummary(
            thread_id=tid, subject=subj[0] if subj else None, members=members,
            last={"from": last[0], "text": last[1], "at": last[2]} if last else {},
            message_count=count, unread=unread)

    async def list_threads(self, address: str | None = None) -> list[ThreadSummary]:
        return [await self._summary(tid) for tid in await self._thread_ids(address)]

    async def thread(self, thread_id: str) -> ThreadDetail:
        rows = await self.db.fetchall(
            "SELECT m.id, a.address, m.subject, m.body, m.content_type, m.created_at "
            "FROM messages m JOIN agents a ON a.id=m.sender_id "
            "WHERE m.thread_id=? ORDER BY m.created_at", (thread_id,))
        messages = []
        members = set()
        for r in rows:
            recs = await self.db.fetchall(
                "SELECT a.address, r.read_at FROM recipients r "
                "JOIN agents a ON a.id=r.agent_id WHERE r.message_id=?", (r[0],))
            to = [x[0] for x in recs]
            read_by = [x[0] for x in recs if x[1]]
            members.add(r[1]); members.update(to)
            messages.append(MessageView(id=r[0], **{"from": r[1]}, to=to,
                                         subject=r[2], body=r[3], content_type=r[4],
                                         created_at=r[5], read_by=read_by))
        subj = rows[0][2] if rows else None
        return ThreadDetail(thread_id=thread_id, subject=subj,
                            members=sorted(members), messages=messages)

    async def create_identity(self, name: str) -> AgentFull:
        # mark as human so the UI tags it "you"; persists (no MCP session to deregister)
        res = await self.agents.register(RegisterAgent(name=name, profile={"human": True}))
        return AgentFull(id=res.id, name=res.name, address=res.address,
                         profile=res.profile, status="online")

    async def send_as(self, from_address: str, to: str, body: str,
                      subject: str | None = None, in_reply_to: str | None = None):
        sender = await self.agents.get_by_address(from_address)
        if sender is None:
            raise ValueError(f"unknown sender: {from_address}")
        return await self.messages.send(sender.id, SendMessage(
            to=to, body=body, subject=subject, in_reply_to=in_reply_to))
