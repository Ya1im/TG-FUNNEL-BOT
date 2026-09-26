"""Проверка подписки пользователя на канал."""
from __future__ import annotations

import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

log = logging.getLogger(__name__)

OK_STATUSES = {"member", "administrator", "creator"}


def _status_name(member) -> str:
    status = getattr(member, "status", "")
    return str(getattr(status, "value", status))


async def probe(bot: Bot, channel_id: str | int, user_id: int) -> bool | None:
    """True — в канале, False — точно нет, None — Telegram не ответил (нет прав, сбой)."""
    try:
        member = await bot.get_chat_member(channel_id, user_id)
    except TelegramAPIError as exc:
        log.warning("Не смог проверить подписку %s в %s: %s", user_id, channel_id, exc)
        return None
    status = _status_name(member)
    if status in OK_STATUSES:
        return True
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


async def is_member(bot: Bot, channel_id: str | int, user_id: int) -> bool:
    """True, если пользователь состоит в канале. Ошибки API считаем «не подписан»."""
    return await probe(bot, channel_id, user_id) is True


class ChannelGate:
    """Проверка подписки с учётом настроек и кэша в БД."""

    def __init__(self, bot: Bot, settings, users) -> None:
        self.bot = bot
        self.settings = settings
        self.users = users
        self._cache: dict[int, tuple[float, bool]] = {}

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

    async def status(self, user_id: int, cached_seconds: int = 0) -> str:
        """«yes» / «no» / «error». Ошибку API не путаем с отпиской и в кэш не пишем.

        cached_seconds > 0 — можно взять недавний результат, чтобы не дёргать Telegram
        на каждом сообщении (рассылки, прогрев). Кнопка «Проверить подписку» кэш не использует."""
        channel = await self.channel_id()
        if channel is None:
            return "yes"
        now = time.time()
        hit = self._cache.get(user_id)
        if cached_seconds > 0 and hit and now - hit[0] < cached_seconds:
            return "yes" if hit[1] else "no"
        result = await probe(self.bot, channel, user_id)
        if result is None:
            return "error"
        await self.users.set_subscription(user_id, result)
        self._cache[user_id] = (now, result)
        return "yes" if result else "no"
