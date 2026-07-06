from postbox.federation import parse_address


def test_parse_address_returns_local_without_domain():
    assert parse_address("alice") == ("alice", None)


def test_parse_address_splits_first_at():
    assert parse_address("alice@postbox2") == ("alice", "postbox2")


def test_parse_address_treats_trailing_empty_domain_as_local():
    assert parse_address("alice@") == ("alice", None)


def test_parse_address_strips_outer_whitespace_and_empty_domain_whitespace():
    assert parse_address("  alice@   ") == ("alice", None)


def test_parse_address_only_splits_once():
    assert parse_address("a@b@c") == ("a", "b@c")
