import pytest
from courier.agents import AgentService
from courier.models import RegisterAgent, Wakeup


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


async def test_register_defaults_name_and_stores_wakeup(db):
    from courier.agents import AgentService
    from courier.models import RegisterAgent
    svc = AgentService(db)
    res = await svc.register(RegisterAgent(wakeup=Wakeup(kind="tmux", target="%9")))
    assert res.address.startswith("copilot-")      # defaulted handle
    row = await db.fetchone(
        "SELECT wakeup_kind,wakeup_target,status FROM agents WHERE id=?", (res.id,))
    assert row == ("tmux", "%9", "online")


async def test_set_name_changes_handle_and_rejects_duplicate(db):
    from courier.agents import AgentService
    from courier.models import RegisterAgent
    svc = AgentService(db)
    a = await svc.register(RegisterAgent())
    b = await svc.register(RegisterAgent())
    renamed = await svc.set_name(a.id, "alice")
    assert renamed.name == "alice" and renamed.address == "alice"
    assert await svc.get_by_address("alice") is not None
    with pytest.raises(ValueError):
        await svc.set_name(b.id, "alice")              # taken


async def test_deregister_and_online_directory(db):
    from courier.agents import AgentService
    from courier.models import RegisterAgent
    svc = AgentService(db)
    a = await svc.register(RegisterAgent())
    b = await svc.register(RegisterAgent())
    await svc.set_status(b.id, "offline")
    online = await svc.directory()                      # online-only by default
    ids = {x.id for x in online}
    assert a.id in ids and b.id not in ids
    await svc.deregister(a.id)
    assert a.id not in {x.id for x in await svc.directory()}
