from pydantic import BaseModel, Field


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


class AgentFull(BaseModel):
    id: str
    name: str
    address: str
    profile: dict | None = None
    status: str = "online"


class ThreadSummary(BaseModel):
    thread_id: str
    subject: str | None
    members: list[str]
    last: dict           # {"from": addr, "text": str, "at": iso}
    message_count: int
    unread: dict[str, int]   # address -> unread count


class MessageView(BaseModel):
    id: str
    from_: str = Field(alias="from")
    to: list[str]
    subject: str | None
    body: str
    content_type: str
    created_at: str
    read_by: list[str]

    model_config = {"populate_by_name": True}


class ThreadDetail(BaseModel):
    thread_id: str
    subject: str | None
    members: list[str]
    messages: list[MessageView]


class SendAs(BaseModel):
    from_: str = Field(alias="from")
    to: str
    body: str
    subject: str | None = None
    in_reply_to: str | None = None

    model_config = {"populate_by_name": True}


class CreateIdentity(BaseModel):
    name: str
