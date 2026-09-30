"""Проверка: принимает ли Telegram кастомные эмодзи в сообщениях этого бота.

Прямого вызова «есть ли у владельца бота Premium» в Bot API нет (бот даже не знает, кто его владелец).
Зато есть честная проверка результатом: шлём сообщение с кастомным эмодзи и смотрим, вернул ли Telegram
его с custom_emoji-сущностью. Если да — у владельца бота есть Premium и эмодзи будут видны людям.
"""
from __future__ import annotations

from bot.content import ContentBlock
from bot.sender import send_block

# Эмодзи из примера в документации Bot API
PROBE_HTML = (
    "Проверка кастомных эмодзи: "
    '<tg-emoji emoji-id="5368324170671202286">👍</tg-emoji>\n'
    "Если вместо обычного 👍 вы видите особый эмодзи, всё работает."
)


async def probe_custom_emoji(bot, chat_id: int) -> bool | None:
    """True — Telegram принял кастомные эмодзи; False — отбросил; None — проверить не удалось."""
    outcome = await send_block(ContentBlock(text=PROBE_HTML), bot, chat_id)
    if not outcome.ok:
        return None
    if not (hasattr(outcome.result, "entities") or hasattr(outcome.result, "caption_entities")):
        return None
    return not outcome.custom_emoji_lost
