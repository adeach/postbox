import pytest
from courier.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "courier.db")
    await d.connect()
    yield d
    await d.close()
