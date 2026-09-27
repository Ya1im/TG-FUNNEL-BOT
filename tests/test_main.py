"""Устойчивость точки входа bot/__main__.py к сетевым сбоям при старте."""
from __future__ import annotations

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError

from bot.__main__ import set_commands
from bot.config import Config

FAKE_TOKEN = "123456789:AAFakeTokenForTestsOnly_0123456789ab"


class _RaisingSession(BaseSession):
    """Любой вызов Telegram API падает с сетевой ошибкой — как при недоступном зеркале."""

    async def close(self) -> None:
        return None

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""

    async def make_request(self, bot, method, timeout=None):
        raise TelegramNetworkError(method=method, message="Request timeout error")


def _config() -> Config:
    return Config(
        bot_token=FAKE_TOKEN,
        admin_ids=(99,),
        db_path=":memory:",
        messages_per_second=20.0,
        tick_seconds=60,
    )


async def test_set_commands_does_not_crash_on_network_error():
    """Сетевой сбой к Telegram (например, недоступное зеркало API) при выставлении команд
    не должен ронять весь процесс бота — раньше именно так падал контейнер на старте
    (bot.set_my_commands для BotCommandScopeDefault не был обёрнут в try/except)."""
    bot = Bot(
        FAKE_TOKEN,
        session=_RaisingSession(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        await set_commands(bot, _config())
    finally:
        await bot.session.close()
