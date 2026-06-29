from pydantic import BaseModel


class Wakeup(BaseModel):
    kind: str = "none"          # 'tmux' | 'os_notify' | 'none'
    target: str | None = None   # e.g. the $TMUX_PANE value


class RegisterAgent(BaseModel):
    name: str | None = None     # v2: optional; server defaults to copilot-<short id>
    address: str | None = None  # v2: optional; defaults to name
    profile: dict | None = None
    wakeup: Wakeup = Wakeup()


class AgentOut(BaseModel):
    id: str
    name: str
    address: str
    profile: dict | None = None
    status: str = "online"


class RegisterResult(AgentOut):
    token: str


class SetName(BaseModel):
    name: str


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
