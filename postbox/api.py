from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

import hmac
from postbox.agents import AgentService
from postbox.auth import make_session_cookie, valid_session_cookie
from postbox.db import Database
from postbox.events import EventBus
from postbox.config import load_settings
from postbox.federation import FederationService
from postbox.fleet import FleetService, Supervisor
from postbox.messages import MessageService
from postbox.peers import PeerService
from postbox.models import AgentOut, RegisterAgent, RegisterResult, SendMessage, SetName
from postbox.models import CreateIdentity, ReadAs, SendAs, FleetAgentIn, FleetAgentOut
from postbox.models import FederationInbound, PeerIn, PeerOut
from postbox.models import TerminalIn, LoginIn
from postbox.observer import ObserverService
from postbox.terminals import TerminalService
import json
import sqlite3


def create_app(data_dir: str | None = None) -> FastAPI:
    settings = load_settings(data_dir)
    db = Database(settings.db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.connect()
        app.state.agents = AgentService(db)
        app.state.bus = EventBus(db)
        app.state.messages = MessageService(db, app.state.agents, app.state.bus)
        app.state.observer = ObserverService(
            db, app.state.agents, app.state.messages, app.state.bus)
        app.state.fleet = FleetService(db, app.state.agents)
        _prog = settings.terminal_cmd.split() if settings.terminal_cmd else None
        app.state.terminals = TerminalService(settings, app.state.agents, program=_prog)
        app.state.terminals.spawn_wait = settings.spawn_wait
        app.state.peers = PeerService(db)
        await app.state.peers.seed(settings.peers_seed)
        app.state.federation = FederationService(
            db, app.state.agents, app.state.messages, app.state.peers,
            app.state.bus, settings)
        app.state.messages.federation = app.state.federation
        app.state.supervisor = Supervisor(db, app.state.bus, app.state.fleet, settings)
        await app.state.supervisor.start()
        yield
        await app.state.supervisor.stop()
        await db.close()

    app = FastAPI(title="Postbox", lifespan=lifespan)

    @app.middleware("http")
    async def no_cache_ui(request, call_next):
        # the UI is a static bundle edited in place; force browsers to revalidate so a
        # CSS/JS change is picked up on the next load instead of serving a stale copy.
        resp = await call_next(request)
        if request.url.path.startswith("/ui"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    async def current_agent(
        authorization: str = Header(default=""),
    ) -> AgentOut:
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer token")
        token = authorization.removeprefix("Bearer ").strip()
        agent = await app.state.agents.resolve_token(token)
        if agent is None:
            raise HTTPException(401, "invalid token")
        return agent

    async def require_observer(request: Request) -> None:
        """Guards /observer + /fleet + /peers. Accepts a UI login session cookie
        (auth.password) OR the X-Observer-Token header/?token= (for curl/scripts/SSE).
        Neither configured → open (localhost/SSH-forward is the gate)."""
        pw = settings.password
        tok = settings.observer_token
        if not pw and not tok:
            return
        if pw and valid_session_cookie(pw, request.cookies.get("postbox_session")):
            return
        if tok:
            got = request.headers.get("x-observer-token") or request.query_params.get("token")
            if got == tok:
                return
        raise HTTPException(401, "authentication required")

    @app.post("/login")
    async def login(payload: LoginIn, response: Response):
        pw = settings.password
        if not pw:
            raise HTTPException(400, "password auth is not enabled")
        if not hmac.compare_digest(payload.password, pw):
            raise HTTPException(401, "wrong password")
        response.set_cookie(
            "postbox_session", make_session_cookie(pw),
            httponly=True, samesite="lax", max_age=30 * 86400, path="/")
        return {"ok": True}

    @app.post("/logout")
    async def logout(response: Response):
        response.delete_cookie("postbox_session", path="/")
        return {"ok": True}

    @app.post("/agents", status_code=201, response_model=RegisterResult)
    async def register(payload: RegisterAgent):
        try:
            return await app.state.agents.register(payload)
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.get("/agents", response_model=list[AgentOut])
    async def directory():
        return await app.state.agents.directory(app.state.bus.online_ids())

    @app.patch("/agents/self", response_model=AgentOut)
    async def set_name(payload: SetName, agent: AgentOut = Depends(current_agent)):
        try:
            return await app.state.agents.set_name(agent.id, payload.name)
        except ValueError as e:
            raise HTTPException(409, str(e))
        except sqlite3.IntegrityError:
            # e.g. a fleet/durable identity (whose address is a referenced key) trying
            # to rename — reject cleanly instead of surfacing a 500.
            raise HTTPException(409, "this identity cannot be renamed")

    @app.delete("/agents/self", status_code=204)
    async def deregister(agent: AgentOut = Depends(current_agent)):
        await app.state.agents.deregister(agent.id)
        return None

    @app.post("/messages", status_code=201)
    async def send(payload: SendMessage, agent: AgentOut = Depends(current_agent)):
        try:
            return await app.state.messages.send(agent.id, payload)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/inbox")
    async def inbox(unread: bool = False, thread: str | None = None,
                    agent: AgentOut = Depends(current_agent)):
        return await app.state.messages.inbox(agent.id, unread=unread, thread=thread)

    @app.get("/messages/{message_id}")
    async def read_message(message_id: str, agent: AgentOut = Depends(current_agent)):
        try:
            return await app.state.messages.read(agent.id, message_id)
        except PermissionError as e:
            raise HTTPException(403, str(e))
        except ValueError as e:
            raise HTTPException(404, str(e))

    @app.get("/threads/{thread_id}")
    async def thread(thread_id: str, agent: AgentOut = Depends(current_agent)):
        return await app.state.messages.thread(agent.id, thread_id)

    @app.get("/observer/agents", dependencies=[Depends(require_observer)])
    async def observer_agents():
        return await app.state.observer.agents_all()

    @app.get("/observer/threads", dependencies=[Depends(require_observer)])
    async def observer_threads(address: str | None = None):
        return await app.state.observer.list_threads(address)

    @app.get("/observer/threads/{thread_id}", dependencies=[Depends(require_observer)])
    async def observer_thread(thread_id: str):
        d = await app.state.observer.thread(thread_id)
        return d.model_dump(by_alias=True)

    @app.post("/observer/identity", status_code=201, dependencies=[Depends(require_observer)])
    async def observer_identity(payload: CreateIdentity):
        try:
            return await app.state.observer.create_identity(payload.name)
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/observer/send", status_code=201, dependencies=[Depends(require_observer)])
    async def observer_send(payload: SendAs):
        try:
            return await app.state.observer.send_as(
                payload.from_, payload.to, payload.body,
                payload.subject, payload.in_reply_to)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/observer/read", dependencies=[Depends(require_observer)])
    async def observer_read(payload: ReadAs):
        try:
            marked = await app.state.observer.mark_thread_read(
                payload.as_, payload.thread_id)
            return {"marked": marked}
        except PermissionError as e:
            raise HTTPException(403, str(e))
        except ValueError as e:
            raise HTTPException(404, str(e))

    @app.delete("/observer/agents/{agent_id}", status_code=204,
                dependencies=[Depends(require_observer)])
    async def observer_forget(agent_id: str):
        # "Forget": hide from the directory but KEEP its messages/threads/session id
        # intact (soft deregister). If it happens to be a fleet agent, stop managing it
        # too, else the Supervisor would keep spawning turns for a now-hidden identity.
        agent = await app.state.agents.get_by_id(agent_id)
        if agent:
            await app.state.supervisor.kill(agent.address)
            await app.state.fleet.remove(agent.address)
        await app.state.agents.deregister(agent_id)
        return None

    @app.post("/terminals", status_code=201, dependencies=[Depends(require_observer)])
    async def terminals_spawn(payload: TerminalIn):
        try:
            return await app.state.terminals.spawn(payload.name, payload.cwd)
        except ValueError as e:
            raise HTTPException(409, str(e))
        except RuntimeError as e:
            raise HTTPException(503, str(e))

    @app.get("/terminals", dependencies=[Depends(require_observer)])
    async def terminals_list():
        return await app.state.terminals.list_terminals()

    @app.delete("/terminals/{name}", status_code=204,
                dependencies=[Depends(require_observer)])
    async def terminals_kill(name: str):
        try:
            await app.state.terminals.kill(name)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except RuntimeError as e:
            raise HTTPException(503, str(e))
        return None

    @app.post("/spawn", status_code=201)
    async def agent_spawn(payload: TerminalIn, agent: AgentOut = Depends(current_agent)):
        # Agent-facing spawn: any registered agent can spin up an interactive copilot and
        # then message it. Waits until the new agent registers so the caller can send
        # to it right away. (The UI uses the observer-guarded /terminals; this is Bearer.)
        # A remote `instance` relays the spawn to that peer (name@instance addressing).
        if payload.instance and payload.instance != settings.instance:
            try:
                return await app.state.federation.spawn_remote(
                    payload.name, payload.cwd, payload.instance)
            except ValueError as e:
                raise HTTPException(404, str(e))
            except Exception as e:
                raise HTTPException(502, f"remote spawn failed: {e}")
        try:
            res = await app.state.terminals.spawn(payload.name, payload.cwd)
        except ValueError as e:
            raise HTTPException(409, str(e))
        except RuntimeError as e:
            raise HTTPException(503, str(e))
        res["registered"] = await app.state.terminals.wait_registered(payload.name)
        return res

    @app.post("/federation/spawn", status_code=201)
    async def federation_spawn(
        payload: TerminalIn,
        x_postbox_peer_token: str = Header(default="", alias="X-Postbox-Peer-Token"),
    ):
        # A peer asks us to spin up a copilot on THIS host (peer-token gated, like inbound).
        if await app.state.federation.peer_by_token(x_postbox_peer_token) is None:
            raise HTTPException(401, "unknown peer token")
        try:
            res = await app.state.terminals.spawn(payload.name, payload.cwd)
        except ValueError as e:
            raise HTTPException(409, str(e))
        except RuntimeError as e:
            raise HTTPException(503, str(e))
        res["registered"] = await app.state.terminals.wait_registered(payload.name)
        return res

    @app.get("/fleet", response_model=list[FleetAgentOut],
             dependencies=[Depends(require_observer)])
    async def fleet_list():
        return await app.state.supervisor.list_status()

    @app.post("/fleet", status_code=201, dependencies=[Depends(require_observer)])
    async def fleet_upsert(payload: FleetAgentIn):
        try:
            await app.state.fleet.upsert(payload.address, payload.command, payload.cwd)
        except ValueError as e:
            raise HTTPException(409, str(e))
        app.state.supervisor.poke()
        return {"ok": True}

    @app.delete("/fleet/{address}", status_code=204,
                dependencies=[Depends(require_observer)])
    async def fleet_remove(address: str):
        await app.state.supervisor.kill(address)
        await app.state.fleet.remove(address)
        return None

    @app.get("/peers", response_model=list[PeerOut],
             dependencies=[Depends(require_observer)])
    async def peers_list():
        return await app.state.peers.list_peers()

    @app.post("/peers", status_code=201, response_model=PeerOut,
              dependencies=[Depends(require_observer)])
    async def peers_upsert(payload: PeerIn):
        await app.state.peers.upsert(payload.name, payload.url, payload.token)
        return {"name": payload.name, "url": payload.url}

    @app.delete("/peers/{name}", status_code=204,
                dependencies=[Depends(require_observer)])
    async def peers_remove(name: str):
        await app.state.peers.remove(name)
        return None

    @app.post("/federation/inbound", status_code=201)
    async def federation_inbound(
        payload: FederationInbound,
        x_postbox_peer_token: str = Header(default="", alias="X-Postbox-Peer-Token"),
    ):
        try:
            return await app.state.federation.inbound(
                x_postbox_peer_token, payload.model_dump(by_alias=True))
        except PermissionError as e:
            detail = str(e)
            if detail == "unknown peer token":
                raise HTTPException(401, detail)
            raise HTTPException(403, detail)
        except LookupError as e:
            raise HTTPException(404, str(e))

    @app.post("/fleet/{address}/enable", dependencies=[Depends(require_observer)])
    async def fleet_enable(address: str):
        await app.state.fleet.set_enabled(address, True)
        app.state.supervisor.poke()
        return {"ok": True}

    @app.post("/fleet/{address}/disable", dependencies=[Depends(require_observer)])
    async def fleet_disable(address: str):
        await app.state.fleet.set_enabled(address, False)
        return {"ok": True}

    @app.post("/fleet/{address}/run", dependencies=[Depends(require_observer)])
    async def fleet_run(address: str):
        try:
            result = await app.state.supervisor.run_now(address)
        except KeyError:
            raise HTTPException(404, f"not a fleet agent: {address}")
        return {"result": result}

    @app.post("/fleet/{address}/kill", dependencies=[Depends(require_observer)])
    async def fleet_kill(address: str):
        killed = await app.state.supervisor.kill(address)
        return {"killed": killed}

    @app.get("/observer/events", dependencies=[Depends(require_observer)])
    async def observer_events(request: Request, last_event_id: int | None = None):
        hdr = request.headers.get("last-event-id")
        start = int(hdr) if hdr else last_event_id
        bus: EventBus = app.state.bus

        async def gen():
            async for ev in bus.stream_all(start):
                yield {"id": str(ev.id), "event": ev.type,
                       "data": json.dumps({**ev.payload, "_id": ev.id, "agent": ev.agent_id})}

        return EventSourceResponse(gen())

    @app.get("/events")
    async def events(request: Request, last_event_id: int | None = None,
                     agent: AgentOut = Depends(current_agent)):
        hdr = request.headers.get("last-event-id")
        start = int(hdr) if hdr else last_event_id
        bus: EventBus = app.state.bus
        agents = app.state.agents
        await agents.set_status(agent.id, "online")

        async def gen():
            try:
                async for ev in bus.stream(agent.id, start):
                    yield {"id": str(ev.id), "event": ev.type,
                           "data": json.dumps({**ev.payload, "_id": ev.id})}
            finally:
                await agents.set_status(agent.id, "offline")

        return EventSourceResponse(gen())

    web_dir = Path(__file__).parent / "web"
    app.mount("/ui", StaticFiles(directory=str(web_dir), html=True), name="ui")

    return app
