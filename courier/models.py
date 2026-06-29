from pydantic import BaseModel


class RegisterAgent(BaseModel):
    name: str
    address: str
    profile: dict | None = None


class AgentOut(BaseModel):
    id: str
    name: str
    address: str
    profile: dict | None = None


class RegisterResult(AgentOut):
    token: str


class SendMessage(BaseModel):
    to: str                              # recipient address (v1: single recipient)
    body: str
    subject: str | None = None
    content_type: str = "text/plain"
    in_reply_to: str | None = None
    idempotency_key: str | None = None


class MessageOut(BaseModel):
    id: str
    thread_id: str
    in_reply_to: str | None
    sender: str                          # sender address
    subject: str | None
    body: str
    content_type: str
    created_at: str
    read_at: str | None = None
