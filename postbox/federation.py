import logging
import sqlite3

import httpx

from postbox.auth import new_id
from postbox.models import MessageOut, SendMessage


def parse_address(to: str) -> tuple[str, str | None]:
    """Return (name, domain) for an address, treating empty domains as local."""
    address = to.strip()
    if "@" not in address:
        return address, None

    name, domain = address.split("@", 1)
    domain = domain.strip()
    return name.strip(), domain or None


class FederationService:
    def __init__(self, db, agents, messages, peers, bus, settings, relay=None,
                 spawn_relay=None):
        self.db = db
        self.agents = agents
        self.messages = messages
        self.peers = peers
        self.bus = bus
        self.settings = settings
        self.relay = relay or self._relay
        self.spawn_relay = spawn_relay or self._relay_spawn
        self.log = logging.getLogger(__name__)

    async def _relay(self, url: str, token: str, payload: dict) -> None:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                f"{url}/federation/inbound",
                headers={"X-Postbox-Peer-Token": token},
                json=payload,
            )
            resp.raise_for_status()

    async def _relay_spawn(self, url: str, token: str, payload: dict) -> dict:
        # a remote spawn boots a copilot AND waits for it to register on the peer, so
        # allow well over the peer's registration timeout before giving up.
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{url}/federation/spawn",
                headers={"X-Postbox-Peer-Token": token},
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()

    async def peer_by_token(self, token: str) -> dict | None:
        for candidate in await self.peers.list_peers():
            if candidate["token"] == token:
                return candidate
        return None

    async def spawn_remote(self, name: str, cwd: str | None, peer_name: str,
                           model: str | None = None) -> dict:
        """Ask a peer to spin up an interactive copilot on ITS host; the spawned agent
        registers on the peer and is addressed as name@peer (existing federation carries
        the chat). Spawn is not soft-success — surface peer errors to the caller."""
        peer = await self.peers.get(peer_name)
        if peer is None:
            raise ValueError(f"unknown peer: {peer_name}")
        result = await self.spawn_relay(peer["url"], peer["token"],
                                        {"name": name, "cwd": cwd, "model": model})
        result["address"] = f"{name}@{peer_name}"
        result["instance"] = peer_name
        return result

    def is_remote(self, to) -> tuple[str, str] | None:
        name, domain = parse_address(to)
        if domain is None or domain == self.settings.instance:
            return None
        return name, domain

    async def send_remote(self, sender_id: str, payload: SendMessage) -> MessageOut:
        remote = self.is_remote(payload.to)
        assert remote is not None
        name, peer_name = remote
        peer = await self.peers.get(peer_name)
        if peer is None:
            raise ValueError(f"unknown peer: {peer_name}")
        if not self.settings.instance:
            raise ValueError("federation not configured: set 'instance' in config.yaml")

        stub_id = await self.agents.ensure_remote(f"{name}@{peer_name}", peer_name)
        subject = payload.subject
        thread_id = new_id()
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

        msg_id = await self.messages._store(
            sender_id=sender_id, recipient_id=stub_id, body=payload.body,
            subject=subject, content_type=payload.content_type, thread_id=thread_id,
            in_reply_to=payload.in_reply_to, idempotency_key=payload.idempotency_key,
            emit_received=False, msg_id=None if payload.in_reply_to else thread_id)
        out = await self.messages.get(sender_id, msg_id)
        sender_addr = (await self.db.fetchone(
            "SELECT address FROM agents WHERE id=?", (sender_id,)))[0]
        relay_body = {
            "from": f"{sender_addr}@{self.settings.instance}",
            "to": name,
            "subject": subject,
            "body": payload.body,
            "content_type": payload.content_type,
            "fed_thread_id": thread_id,
            "origin_msg_id": msg_id,
            "created_at": out.created_at,
        }
        try:
            await self.relay(peer["url"], peer["token"], relay_body)
        except Exception:
            self.log.exception("federation relay failed")
        return out

    async def inbound(self, peer_token: str, body: dict) -> dict:
        peer = await self.peer_by_token(peer_token)
        if peer is None:
            raise PermissionError("unknown peer token")

        _, fdomain = parse_address(body["from"])
        if fdomain != peer["name"]:
            raise PermissionError("from-domain does not match relaying peer")

        try:
            stub_id = await self.agents.ensure_remote(body["from"], fdomain)
        except sqlite3.IntegrityError:
            stub = await self.agents.get_by_address(body["from"])
            if stub is None:
                raise
            stub_id = stub.id

        recipient = await self.agents.get_by_address(body["to"])
        if recipient is None:
            raise LookupError(f"unknown local recipient: {body['to']}")

        msg_id = await self.messages._store(
            sender_id=stub_id, recipient_id=recipient.id, body=body["body"],
            subject=body.get("subject"),
            content_type=body.get("content_type", "text/plain"),
            thread_id=body["fed_thread_id"], in_reply_to=None,
            idempotency_key=body["origin_msg_id"], emit_received=True)
        return {"message_id": msg_id, "thread_id": body["fed_thread_id"]}
