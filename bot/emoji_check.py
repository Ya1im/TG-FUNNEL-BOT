"""Проверка: принимает ли Telegram кастомные эмодзи в сообщениях этого бота.

Прямого вызова «есть ли у владельца бота Premium» в Bot API нет (бот даже не знает, кто его владелец).
Зато есть честная проверка результатом: шлём сообщение с кастомным эмодзи и смотрим, вернул ли Telegram
его с custom_emoji-сущностью. Если да — у владельца бота есть Premium и эмодзи будут видны людям.

Эмодзи для пробы берём живым из Telegram (getForumTopicIconStickers): устаревший захардкоженный id дал бы
ложное «не работают», потому что Telegram молча отбрасывает сущность с несуществующим id.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from bot.content import ContentBlock
from bot.sender import send_block

# Эмодзи из примера в документации Bot API — запасной вариант
DOC_EMOJI_ID = "5368324170671202286"


@dataclass
class ProbeResult:
    ok: bool | None                     # True — принял, False — отбросил, None — проверить не удалось
    emoji_id: str = DOC_EMOJI_ID
    source: str = "docs"                # telegram — живой id из Telegram; docs — из документации
    entity_types: list[str] = field(default_factory=list)   # какие сущности вернул Telegram в ответе


async def pick_probe_emoji(bot) -> tuple[str, str, str]:
    """(id, запасной символ, источник)."""
    try:
        stickers = await bot.get_forum_topic_icon_stickers()
        if isinstance(stickers, list):
            for sticker in stickers:
                emoji_id = getattr(sticker, "custom_emoji_id", None)
                if emoji_id:
                    return str(emoji_id), getattr(sticker, "emoji", None) or "👍", "telegram"
    except Exception:  # noqa: BLE001 — проверка не должна падать из-за недоступного справочника
        pass
    return DOC_EMOJI_ID, "👍", "docs"


def _entity_types(result) -> list[str]:
    entities = list(getattr(result, "entities", None) or []) + list(getattr(result, "caption_entities", None) or [])
    return [str(getattr(entity, "type", "")) for entity in entities]


async def probe(bot, chat_id: int) -> ProbeResult:
    emoji_id, symbol, source = await pick_probe_emoji(bot)
    html = (
        "Проверка кастомных эмодзи: "
        f'<tg-emoji emoji-id="{emoji_id}">{symbol}</tg-emoji>\n'
        f"Если вместо обычного {symbol} вы видите особый эмодзи, всё работает."
    )
    outcome = await send_block(ContentBlock(text=html), bot, chat_id)
    result = ProbeResult(ok=None, emoji_id=emoji_id, source=source)
    if not outcome.ok:
        return result
    if not (hasattr(outcome.result, "entities") or hasattr(outcome.result, "caption_entities")):
        return result
    result.entity_types = _entity_types(outcome.result)
    result.ok = not outcome.custom_emoji_lost
    return result


async def probe_custom_emoji(bot, chat_id: int) -> bool | None:
    return (await probe(bot, chat_id)).ok
