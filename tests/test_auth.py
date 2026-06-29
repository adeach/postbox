from postbox.auth import new_id, now_iso, generate_token, hash_token


def test_new_id_unique():
    assert new_id() != new_id()
    assert len(new_id()) >= 16


def test_now_iso_utc():
    assert now_iso().endswith("Z")


def test_token_hash_is_stable_and_matches():
    tok = generate_token()
    assert len(tok) >= 32
    assert hash_token(tok) == hash_token(tok)
    assert hash_token(tok) != hash_token(generate_token())
