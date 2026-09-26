"""Напоминание подписаться на канал — шлётся только там, где админ сам это включил."""
from __future__ import annotations

from bot.content import apply_placeholders
from bot.keyboards import subscribe_kb
from bot.sender import safe_send


async def send_reminder(bot, settings, users, limiter, user_id: int, key: str = "reminder_text"):
    text = apply_placeholders(await settings.get(key), await settings.get("channel_url"))
    kb = await subscribe_kb(settings)

    async def action():
        return await bot.send_message(user_id, text, reply_markup=kb)

    return await safe_send(action, chat_id=user_id, users=users, limiter=limiter)
