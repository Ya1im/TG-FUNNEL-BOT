"""Клавиатуры пользовательской части."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

CHECK_CALLBACK = "check_sub"


async def subscribe_kb(settings) -> InlineKeyboardMarkup:
    """Кнопки «Подписаться» (ссылка на канал) и «Я подписан» (проверка)."""
    url = (await settings.get("channel_url")).strip()
    rows = []
    if url:
        rows.append([InlineKeyboardButton(text=await settings.get("btn_subscribe"), url=url)])
    rows.append(
        [InlineKeyboardButton(text=await settings.get("btn_check"), callback_data=CHECK_CALLBACK)]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def link_kb(text: str, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, url=url)]])
