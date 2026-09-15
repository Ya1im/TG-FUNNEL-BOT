"""Проверка подписки пользователя на канал."""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

log = logging.getLogger(__name__)

OK_STATUSES = {"member", "administrator", "creator"}


def _status_name(member) -> str:
    status = getattr(member, "status", "")
    return str(getattr(status, "value", status))


async def is_member(bot: Bot, channel_id: str | int, user_id: int) -> bool:
    """True, если пользователь состоит в канале. Ошибки API считаем «не подписан»."""
    try:
        member = await bot.get_chat_member(channel_id, user_id)
    except TelegramAPIError as exc:
        log.warning("Не смог проверить подписку %s в %s: %s", user_id, channel_id, exc)
        return False
    status = _status_name(member)
    if status in OK_STATUSES:
        return True
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


class ChannelGate:
    """Проверка подписки с учётом настроек и кэша в БД."""

    def __init__(self, bot: Bot, settings, users) -> None:
        self.bot = bot
        self.settings = settings
        self.users = users

    async def channel_id(self) -> str | int | None:
        raw = (await self.settings.get("channel_id")).strip()
        if not raw:
            return None
        if raw.lstrip("-").isdigit():
            return int(raw)
        return raw if raw.startswith("@") else f"@{raw}"

    async def check(self, user_id: int) -> bool:
        """Если канал не настроен — гейт открыт, иначе спрашиваем Telegram."""
        channel = await self.channel_id()
        if channel is None:
            return True
        ok = await is_member(self.bot, channel, user_id)
        await self.users.set_subscription(user_id, ok)
        return ok
