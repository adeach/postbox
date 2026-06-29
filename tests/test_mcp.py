import pytest
from httpx import ASGITransport, AsyncClient
from courier.api import create_app
from courier.mcp_server import MailTools


@pytest.fixture
async def tools(tmp_path):
    app = create_app(str(tmp_path / "data"))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            a = (await c.post("/agents", json={"name": "A", "address": "a"})).json()
            b = (await c.post("/agents", json={"name": "B", "address": "b"})).json()
            yield MailTools(c, a["token"]), MailTools(c, b["token"])


async def test_list_agents_tool(tools):
    a_mail, _ = tools
    agents = await a_mail.list_agents()
    assert {x["address"] for x in agents} == {"a", "b"}


async def test_send_then_recipient_sees_in_inbox(tools):
    a_mail, b_mail = tools
    await a_mail.send_message(to="b", body="hi", subject="s")
    inbox = await b_mail.check_inbox(unread=True)
    assert [m["body"] for m in inbox] == ["hi"]


async def test_reply_threads_and_routes_back(tools):
    a_mail, b_mail = tools
    m = await a_mail.send_message(to="b", body="q", subject="Q")
    r = await b_mail.reply(message_id=m["id"], body="re")  # B reads original, replies to A
    assert r["thread_id"] == m["thread_id"]
    a_inbox = await a_mail.check_inbox(unread=True)
    assert [x["body"] for x in a_inbox] == ["re"]
