import pytest
from courier.agents import AgentService
from courier.models import RegisterAgent


async def test_register_returns_token_and_lists_in_directory(db):
    svc = AgentService(db)
    res = await svc.register(RegisterAgent(name="Claude", address="claude"))
    assert res.token
    assert res.address == "claude"

    directory = await svc.directory()
    assert any(a.address == "claude" for a in directory)
    # directory must NOT leak tokens
    assert not hasattr(directory[0], "token")


async def test_duplicate_address_rejected(db):
    svc = AgentService(db)
    await svc.register(RegisterAgent(name="A", address="dup"))
    with pytest.raises(ValueError):
        await svc.register(RegisterAgent(name="B", address="dup"))


async def test_resolve_token(db):
    svc = AgentService(db)
    res = await svc.register(RegisterAgent(name="A", address="a"))
    agent = await svc.resolve_token(res.token)
    assert agent is not None and agent.address == "a"
    assert await svc.resolve_token("bogus") is None
