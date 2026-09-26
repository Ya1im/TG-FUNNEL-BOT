import importlib
import sys
from pathlib import Path

import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.db import Database  # noqa: E402

# Роутеры aiogram — модульные синглтоны (на процесс один диспетчер).
# Чтобы каждый тест получал свой чистый диспетчер, пересобираем модули заново.
ROUTER_MODULES = [
    "bot.handlers.admin.common",
    "bot.handlers.admin.flow",
    "bot.handlers.admin.media",
    "bot.handlers.admin.funnel",
    "bot.handlers.admin.material",
    "bot.handlers.admin.repeat_start",
    "bot.handlers.admin.settings",
    "bot.handlers.admin.stats",
    "bot.handlers.admin.broadcast",
    "bot.handlers.admin.capture",
    "bot.handlers.admin.fallback",
    "bot.handlers.admin",
    "bot.handlers.user",
    "bot.handlers",
    "bot.__main__",
]


def fresh_dispatcher(deps):
    for name in ROUTER_MODULES:
        importlib.reload(importlib.import_module(name))
    return importlib.import_module("bot.__main__").build_dispatcher(deps)


@pytest_asyncio.fixture
async def db(tmp_path):
    database = await Database(tmp_path / "test.db").connect()
    yield database
    await database.close()
