from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from postbox.agents import AgentService
from postbox.db import Database
from postbox.events import EventBus
from postbox.config import load_settings
from postbox.messages import MessageService
from postbox.models import AgentOut, RegisterAgent, RegisterResult, SendMessage, SetName
from postbox.models import CreateIdentity, ReadAs, SendAs
from postbox.observer import ObserverService
import json


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
        yield
        await db.close()

    app = FastAPI(title="Postbox", lifespan=lifespan)

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

    @app.get("/observer/agents")
    async def observer_agents():
        return await app.state.observer.agents_all()

    @app.get("/observer/threads")
    async def observer_threads(address: str | None = None):
        return await app.state.observer.list_threads(address)

    @app.get("/observer/threads/{thread_id}")
    async def observer_thread(thread_id: str):
        d = await app.state.observer.thread(thread_id)
        return d.model_dump(by_alias=True)

    @app.post("/observer/identity", status_code=201)
    async def observer_identity(payload: CreateIdentity):
        try:
            return await app.state.observer.create_identity(payload.name)
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/observer/send", status_code=201)
    async def observer_send(payload: SendAs):
        try:
            return await app.state.observer.send_as(
                payload.from_, payload.to, payload.body,
                payload.subject, payload.in_reply_to)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/observer/read")
    async def observer_read(payload: ReadAs):
        try:
            marked = await app.state.observer.mark_thread_read(
                payload.as_, payload.thread_id)
            return {"marked": marked}
        except PermissionError as e:
            raise HTTPException(403, str(e))
        except ValueError as e:
            raise HTTPException(404, str(e))

    @app.get("/observer/events")
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
