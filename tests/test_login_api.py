import pytest
from httpx import ASGITransport, AsyncClient

from postbox.api import create_app


def _app(tmp_path, cfg=""):
    data = tmp_path / "data"
    data.mkdir()
    if cfg:
        (data / "config.yaml").write_text(cfg)
    return create_app(str(data))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("POSTBOX_PASSWORD", raising=False)
    monkeypatch.delenv("POSTBOX_OBSERVER_TOKEN", raising=False)


async def test_password_gate_login_logout(tmp_path):
    app = _app(tmp_path, "auth:\n  password: adeesh\n")
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.get("/observer/agents")).status_code == 401   # gated
            assert (await c.post("/login", json={"password": "nope"})).status_code == 401
            assert (await c.post("/login", json={"password": "adeesh"})).status_code == 200
            # the client now holds the session cookie → guarded route works
            assert (await c.get("/observer/agents")).status_code == 200
            await c.post("/logout")
            c.cookies.clear()
            assert (await c.get("/observer/agents")).status_code == 401   # gated again


async def test_open_when_no_auth_configured(tmp_path):
    app = _app(tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.get("/observer/agents")).status_code == 200   # localhost is the gate
            assert (await c.post("/login", json={"password": "x"})).status_code == 400


async def test_token_still_works_alongside_password(tmp_path):
    app = _app(tmp_path, "auth:\n  password: adeesh\nobserver_token: tok123\n")
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.get("/observer/agents")).status_code == 401
            # curl/scripts path: the token header still authenticates
            r = await c.get("/observer/agents", headers={"X-Observer-Token": "tok123"})
            assert r.status_code == 200
