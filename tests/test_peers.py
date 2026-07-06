from postbox.peers import PeerService


async def test_peer_crud(db):
    peers = PeerService(db)

    await peers.upsert("east", "http://east.example", "tok1")
    assert await peers.get("east") == {
        "name": "east",
        "url": "http://east.example",
        "token": "tok1",
    }
    assert await peers.list_peers() == [{
        "name": "east",
        "url": "http://east.example",
        "token": "tok1",
    }]

    await peers.upsert("east", "http://east2.example", "tok2")
    assert await peers.get("east") == {
        "name": "east",
        "url": "http://east2.example",
        "token": "tok2",
    }

    await peers.remove("east")
    assert await peers.get("east") is None
    assert await peers.list_peers() == []


async def test_list_peers_orders_by_name(db):
    peers = PeerService(db)
    await peers.upsert("west", "http://west.example", "west-token")
    await peers.upsert("east", "http://east.example", "east-token")

    assert [p["name"] for p in await peers.list_peers()] == ["east", "west"]


async def test_seed_is_idempotent_and_does_not_overwrite_runtime_edits(db):
    peers = PeerService(db)
    seed = ({"name": "east", "url": "http://seed.example", "token": "seed-token"},)

    await peers.seed(seed)
    await peers.seed(seed)
    assert await peers.list_peers() == [{
        "name": "east",
        "url": "http://seed.example",
        "token": "seed-token",
    }]

    await peers.upsert("east", "http://runtime.example", "runtime-token")
    await peers.seed(seed)
    assert await peers.get("east") == {
        "name": "east",
        "url": "http://runtime.example",
        "token": "runtime-token",
    }
