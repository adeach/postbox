from postbox.agents import AgentService
from postbox.auth import new_id, now_iso
from postbox.db import Database
from postbox.events import EventBus
from postbox.models import MessageOut, SendMessage


class MessageService:
    def __init__(self, db: Database, agents: AgentService, bus: EventBus):
        self.db = db
        self.agents = agents
        self.bus = bus
        self.federation = None

    async def _row_to_out(self, row, read_at=None) -> MessageOut:
        sender_addr = (await self.db.fetchone(
            "SELECT address FROM agents WHERE id=?", (row[3],)))[0]
        return MessageOut(
            id=row[0], thread_id=row[1], in_reply_to=row[2], sender=sender_addr,
            subject=row[4], body=row[5], content_type=row[6], created_at=row[8],
            read_at=read_at,
        )

    async def _store(self, *, sender_id: str, recipient_id: str, body: str,
                     subject: str | None, content_type: str, thread_id: str,
                     in_reply_to: str | None, idempotency_key: str | None,
                     emit_received: bool = True, msg_id: str | None = None) -> str:
        # idempotency: return existing message for a repeated key
        if idempotency_key:
            existing = await self.db.fetchone(
                "SELECT id FROM messages WHERE sender_id=? AND idempotency_key=?",
                (sender_id, idempotency_key),
            )
            if existing:
                return existing[0]

        msg_id = msg_id or new_id()
        created = now_iso()
        async with self.db.write_lock:
            await self.db.conn.execute(
                "INSERT INTO messages(id,thread_id,in_reply_to,sender_id,subject,"
                "body,content_type,idempotency_key,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (msg_id, thread_id, in_reply_to, sender_id, subject,
                 body, content_type, idempotency_key, created),
            )
            await self.db.conn.execute(
                "INSERT INTO recipients(message_id,agent_id,kind,delivered_at) "
                "VALUES (?,?,?,?)",
                (msg_id, recipient_id, "to", created),
            )
            await self.db.conn.commit()

        if emit_received:
            sender_addr = (await self.db.fetchone(
                "SELECT address FROM agents WHERE id=?", (sender_id,)))[0]
            ev = await self.bus.append(recipient_id, "message.received", {
                "message_id": msg_id, "thread_id": thread_id,
                "from": sender_addr, "subject": subject,
            })
            await self.bus.publish(ev)
        return msg_id

    async def send(self, sender_id: str, payload: SendMessage) -> MessageOut:
        if self.federation is not None and self.federation.is_remote(payload.to):
            return await self.federation.send_remote(sender_id, payload)

        # idempotency: return existing message for a repeated key
        if payload.idempotency_key:
            existing = await self.db.fetchone(
                "SELECT id FROM messages WHERE sender_id=? AND idempotency_key=?",
                (sender_id, payload.idempotency_key),
            )
            if existing:
                return await self.get(sender_id, existing[0])

        recipient = await self.agents.get_by_address(payload.to)
        if recipient is None:
            raise ValueError(f"unknown recipient: {payload.to}")

        msg_id = new_id()
        subject = payload.subject
        thread_id = msg_id
        if payload.in_reply_to:
            parent = await self.db.fetchone(
                "SELECT thread_id,subject FROM messages WHERE id=?",
                (payload.in_reply_to,),
            )
            if parent is None:
                raise ValueError(f"in_reply_to not found: {payload.in_reply_to}")
            thread_id = parent[0]
            if subject is None:
                subject = parent[1]

        stored_id = await self._store(
            sender_id=sender_id, recipient_id=recipient.id, body=payload.body,
            subject=subject, content_type=payload.content_type, thread_id=thread_id,
            in_reply_to=payload.in_reply_to, idempotency_key=payload.idempotency_key,
            emit_received=True, msg_id=msg_id)
        return await self.get(sender_id, stored_id)

    async def _load(self, agent_id: str, message_id: str, mark_read: bool) -> MessageOut:
        """Participant view. A participant is the sender OR a recipient.
        When mark_read and the caller is an UNREAD recipient, mark it read and
        emit message.read to the sender. The sender viewing their own message
        never marks anything (rec is None for the sender)."""
        row = await self.db.fetchone("SELECT * FROM messages WHERE id=?", (message_id,))
        if row is None:
            raise ValueError("message not found")
        rec = await self.db.fetchone(
            "SELECT read_at FROM recipients WHERE message_id=? AND agent_id=?",
            (message_id, agent_id),
        )
        is_sender = row[3] == agent_id
        if rec is None and not is_sender:
            raise PermissionError("not a participant")
        read_at = rec[0] if rec else None
        if mark_read and rec is not None and rec[0] is None:
            read_at = now_iso()
            await self.db.execute(
                "UPDATE recipients SET read_at=? WHERE message_id=? AND agent_id=?",
                (read_at, message_id, agent_id),
            )
            reader_addr = (await self.db.fetchone(
                "SELECT address FROM agents WHERE id=?", (agent_id,)))[0]
            ev = await self.bus.append(row[3], "message.read",
                                       {"message_id": message_id, "by": reader_addr})
            await self.bus.publish(ev)
        return await self._row_to_out(row, read_at=read_at)

    async def get(self, agent_id: str, message_id: str) -> MessageOut:
        """View without marking read (used internally, e.g. idempotent resend)."""
        return await self._load(agent_id, message_id, mark_read=False)

    async def read(self, agent_id: str, message_id: str) -> MessageOut:
        """View and, for an unread recipient, mark read + emit receipt."""
        return await self._load(agent_id, message_id, mark_read=True)

    async def inbox(self, agent_id: str, unread: bool = False,
                    thread: str | None = None) -> list[MessageOut]:
        sql = (
            "SELECT m.*, r.read_at FROM messages m "
            "JOIN recipients r ON r.message_id=m.id "
            "WHERE r.agent_id=?"
        )
        params: list = [agent_id]
        if unread:
            sql += " AND r.read_at IS NULL"
        if thread:
            sql += " AND m.thread_id=?"
            params.append(thread)
        sql += " ORDER BY m.created_at"
        rows = await self.db.fetchall(sql, tuple(params))
        return [await self._row_to_out(r[:9], read_at=r[9]) for r in rows]

    async def thread(self, agent_id: str, thread_id: str) -> list[MessageOut]:
        rows = await self.db.fetchall(
            "SELECT * FROM messages WHERE thread_id=? ORDER BY created_at", (thread_id,)
        )
        out = []
        for row in rows:
            rec = await self.db.fetchone(
                "SELECT read_at FROM recipients WHERE message_id=? AND agent_id=?",
                (row[0], agent_id),
            )
            if rec is None and row[3] != agent_id:
                continue  # only show messages the agent participates in
            out.append(await self._row_to_out(row, read_at=rec[0] if rec else None))
        return out
