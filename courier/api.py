from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from courier.agents import AgentService
from courier.db import Database
from courier.events import EventBus
from courier.config import load_settings
from courier.messages import MessageService
from courier.models import AgentOut, RegisterAgent, RegisterResult, SendMessage
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
        yield
        await db.close()

    app = FastAPI(title="Courier", lifespan=lifespan)

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
        return await app.state.agents.directory()

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

    @app.get("/events")
    async def events(request: Request, last_event_id: int | None = None,
                     agent: AgentOut = Depends(current_agent)):
        # honor Last-Event-ID header if present
        hdr = request.headers.get("last-event-id")
        start = int(hdr) if hdr else last_event_id
        bus: EventBus = app.state.bus

        async def gen():
            async for ev in bus.stream(agent.id, start):
                yield {"id": str(ev.id), "event": ev.type,
                       "data": json.dumps({**ev.payload, "_id": ev.id})}

        return EventSourceResponse(gen())

    return app
