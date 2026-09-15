import sys
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.db import Database  # noqa: E402


@pytest_asyncio.fixture
async def db(tmp_path):
    database = await Database(tmp_path / "test.db").connect()
    yield database
    await database.close()
