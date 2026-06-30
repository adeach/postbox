import asyncio
import json
from dataclasses import dataclass

from postbox.auth import now_iso
from postbox.db import Database


@dataclass
class Event:
    id: int
    agent_id: str
    type: str
    payload: dict
    created_at: str


class EventBus:
    """Durable event log (SQLite) + in-process pub/sub for SSE.

    Ordering authority is the monotonic events.id. The SSE handoff in `stream`
    subscribes to the live queue FIRST, then replays from the log, then flushes
    the queue while de-duplicating anything already replayed — avoiding the
    gap-drop / duplicate race.
    """

    def __init__(self, db: Database):
        self.db = db
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._firehose: set[asyncio.Queue] = set()

    async def append(self, agent_id: str, type: str, payload: dict) -> Event:
        created = now_iso()
        async with self.db.write_lock:
            cur = await self.db.conn.execute(
                "INSERT INTO events(agent_id,type,payload,created_at) VALUES (?,?,?,?)",
                (agent_id, type, json.dumps(payload), created),
            )
            await self.db.conn.commit()
            event_id = cur.lastrowid
        return Event(event_id, agent_id, type, payload, created)

    async def load_after(self, agent_id: str, after_id: int) -> list[Event]:
        rows = await self.db.fetchall(
            "SELECT id,agent_id,type,payload,created_at FROM events "
            "WHERE agent_id=? AND id>? ORDER BY id",
            (agent_id, after_id),
        )
        return [Event(r[0], r[1], r[2], json.loads(r[3]), r[4]) for r in rows]

    def subscribe(self, agent_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(agent_id, set()).add(q)
        return q

    def unsubscribe(self, agent_id: str, q: asyncio.Queue) -> None:
        subs = self._subs.get(agent_id)
        if subs:
            subs.discard(q)
            if not subs:
                self._subs.pop(agent_id, None)

    def is_online(self, agent_id: str) -> bool:
        """Live presence: an agent is online iff it holds >=1 live SSE subscription.
        Reference-counted for free (set of queues); empty after a restart, so no
        ghost-online and no heartbeat/TTL needed."""
        return bool(self._subs.get(agent_id))

    def online_ids(self) -> set[str]:
        return {aid for aid, qs in self._subs.items() if qs}

    async def publish(self, event: Event) -> None:
        for q in list(self._subs.get(event.agent_id, ())):
            await q.put(event)
        for q in list(self._firehose):
            await q.put(event)

    def subscribe_all(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._firehose.add(q)
        return q

    def unsubscribe_all(self, q: asyncio.Queue) -> None:
        self._firehose.discard(q)

    async def load_all_after(self, after_id: int) -> list[Event]:
        rows = await self.db.fetchall(
            "SELECT id,agent_id,type,payload,created_at FROM events "
            "WHERE id>? ORDER BY id",
            (after_id,),
        )
        return [Event(r[0], r[1], r[2], json.loads(r[3]), r[4]) for r in rows]

    async def stream_all(self, last_event_id: int | None):
        after = last_event_id or 0
        q = self.subscribe_all()                       # live first
        try:
            replayed_max = after
            for ev in await self.load_all_after(after): # replay backlog
                yield ev
                replayed_max = ev.id
            while True:                                 # flush live, dedup
                ev = await q.get()
                if ev.id <= replayed_max:
                    continue
                yield ev
                replayed_max = ev.id
        finally:
            self.unsubscribe_all(q)

    async def stream(self, agent_id: str, last_event_id: int | None):
        after = last_event_id or 0
        q = self.subscribe(agent_id)              # (1) live first — buffer concurrent events
        try:
            replayed_max = after
            for ev in await self.load_after(agent_id, after):   # (2) replay backlog
                yield ev
                replayed_max = ev.id
            while True:                            # (3) flush live, dedup <= replayed_max
                ev = await q.get()
                if ev.id <= replayed_max:
                    continue
                yield ev
                replayed_max = ev.id
        finally:
            self.unsubscribe(agent_id, q)
