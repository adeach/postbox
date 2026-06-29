import pytest
from postbox.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "postbox.db")
    await d.connect()
    yield d
    await d.close()
