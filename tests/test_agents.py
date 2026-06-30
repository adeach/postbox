import pytest
from postbox.agents import AgentService
from postbox.models import RegisterAgent, Wakeup


async def test_register_returns_token_and_lists_in_directory(db):
    svc = AgentService(db)
    res = await svc.register(RegisterAgent(name="Claude", address="claude"))
    assert res.token
    assert res.address == "claude"

    directory = await svc.directory(online_ids={res.id})  # claude has a live connection
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
    from postbox.agents import AgentService
    from postbox.models import RegisterAgent
    svc = AgentService(db)
    res = await svc.register(RegisterAgent(wakeup=Wakeup(kind="tmux", target="%9")))
    assert res.address.startswith("copilot-")      # defaulted handle
    row = await db.fetchone(
        "SELECT wakeup_kind,wakeup_target,status FROM agents WHERE id=?", (res.id,))
    assert row == ("tmux", "%9", "online")


async def test_set_name_changes_handle_and_rejects_duplicate(db):
    from postbox.agents import AgentService
    from postbox.models import RegisterAgent
    svc = AgentService(db)
    a = await svc.register(RegisterAgent())
    b = await svc.register(RegisterAgent())
    renamed = await svc.set_name(a.id, "alice")
    assert renamed.name == "alice" and renamed.address == "alice"
    assert await svc.get_by_address("alice") is not None
    with pytest.raises(ValueError):
        await svc.set_name(b.id, "alice")              # taken


async def test_directory_annotates_live_status_not_stored_latch(db):
    """Directory lists all identities with TRUTHFUL live presence (from online_ids),
    ignoring the stored 'online' latch that register() writes."""
    from postbox.agents import AgentService
    from postbox.models import RegisterAgent
    svc = AgentService(db)
    a = await svc.register(RegisterAgent())          # register writes status='online'...
    b = await svc.register(RegisterAgent())
    dir_ = {x.id: x for x in await svc.directory(online_ids={a.id})}
    assert dir_[a.id].status == "online"             # ...but live presence says: a online
    assert dir_[b.id].status == "offline"            # b holds no SSE connection -> offline
    # with nobody connected (e.g. after a restart) everyone is offline
    none_live = {x.id: x for x in await svc.directory(online_ids=set())}
    assert none_live[a.id].status == "offline" and none_live[b.id].status == "offline"
