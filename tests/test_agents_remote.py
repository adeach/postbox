from postbox.agents import AgentService


async def test_ensure_remote_creates_stub_profile_and_directory_entry(db):
    svc = AgentService(db)

    agent_id = await svc.ensure_remote("alice@postbox2", "postbox2")

    agent = await svc.get_by_address("alice@postbox2")
    assert agent is not None
    assert agent.id == agent_id
    assert agent.name == "alice@postbox2"
    assert agent.profile == {"remote": True, "peer": "postbox2"}

    directory = {a.address: a for a in await svc.directory(online_ids=set())}
    assert directory["alice@postbox2"].status == "offline"


async def test_ensure_remote_is_idempotent_and_does_not_duplicate_rows(db):
    svc = AgentService(db)

    first_id = await svc.ensure_remote("alice@postbox2", "postbox2")
    second_id = await svc.ensure_remote("alice@postbox2", "postbox2")

    assert second_id == first_id
    row = await db.fetchone(
        "SELECT COUNT(*) FROM agents WHERE address=?", ("alice@postbox2",))
    assert row[0] == 1
