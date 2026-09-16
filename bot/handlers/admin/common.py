"""Общее для админки: фильтр, навигация, разбор контента и кнопок."""
from __future__ import annotations

import json
import time

from dataclasses import dataclass

from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from bot.content import extract_media

BACK = "⬅️ Назад"


class AdminFilter(BaseFilter):
    async def __call__(self, event: TelegramObject, deps=None) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user and deps and deps.config.is_admin(user.id))


class MediaAdd(StatesGroup):
    waiting_file = State()
    waiting_slug = State()


class FunnelAdd(StatesGroup):
    waiting_delay = State()
    waiting_content = State()
    waiting_buttons = State()


class FunnelEditDelay(StatesGroup):
    waiting_value = State()


class FunnelImport(StatesGroup):
    waiting_file = State()


class MaterialAdd(StatesGroup):
    waiting_content = State()
    waiting_buttons = State()


class SettingEdit(StatesGroup):
    waiting_value = State()


class ChannelSet(StatesGroup):
    waiting_value = State()


class ReportChatSet(StatesGroup):
    """Чат, куда пересылаем таблицу статистики («📤 Переслать эксперту»)."""
    waiting_value = State()


class BroadcastNew(StatesGroup):
    collecting = State()
    waiting_schedule = State()


class MediaCapture(StatesGroup):
    """Файлы, присланные админом мимо сценария «Медиатека» — копятся тут перед сохранением."""

    collecting = State()
    picking = State()


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    """rows: [[(текст, callback_data), ...], ...]"""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


@dataclass
class MenuRef:
    """Координаты уже показанного меню-сообщения — чтобы отредактировать его, а не слать новое.

    Нужна там, где следующий шаг приходит не кнопкой, а обычным сообщением от админа:
    Telegram не даёт превратить чужое сообщение в правку старого, поэтому храним, где
    висит меню, и правим именно его.
    """

    bot: object
    chat_id: int
    message_id: int


async def show(target: "Message | CallbackQuery | MenuRef", text: str, markup=None) -> Message | None:
    """Показать экран: правим сообщение меню, если оно есть, иначе шлём новое."""
    if isinstance(target, CallbackQuery):
        bot, chat_id, message_id = target.message.bot, target.message.chat.id, target.message.message_id
    elif isinstance(target, MenuRef):
        bot, chat_id, message_id = target.bot, target.chat_id, target.message_id
    else:
        return await target.answer(text, reply_markup=markup, disable_web_page_preview=True)

    try:
        return await bot.edit_message_text(
            text, chat_id=chat_id, message_id=message_id, reply_markup=markup,
            disable_web_page_preview=True,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return None  # экран не изменился — второе сообщение не нужно
    except Exception:  # noqa: BLE001 — например, редактируем медиа-сообщение (нет текста)
        pass
    try:
        return await bot.edit_message_caption(
            chat_id=chat_id, message_id=message_id, caption=text, reply_markup=markup,
        )
    except Exception:  # noqa: BLE001 — не вышло (сообщение слишком старое/удалено) — шлём новое
        pass
    return await bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)


def message_text(message: Message) -> str | None:
    """Текст сообщения с сохранением форматирования."""
    if message.text or message.caption:
        return message.html_text
    return None


async def capture_content(deps, message: Message, slug_prefix: str) -> tuple[str | None, int | None]:
    """Достать из сообщения админа текст и медиа (медиа сохраняем в медиатеку)."""
    text = message_text(message)
    found = extract_media(message)
    media_id = None
    if found:
        kind, file_id, file_unique_id = found
        slug = f"{slug_prefix}_{int(time.time())}"
        media_id = await deps.media.save(slug, kind, file_id, file_unique_id)
    return text, media_id


def parse_buttons(raw: str) -> list[dict]:
    """«Текст | https://ссылка» построчно."""
    buttons = []
    for line in (raw or "").splitlines():
        if "|" not in line:
            continue
        text, _, url = line.partition("|")
        text, url = text.strip(), url.strip()
        if text and url.startswith("http"):
            buttons.append({"text": text, "url": url})
    return buttons


def buttons_hint(buttons_json: str | None) -> str:
    try:
        buttons = json.loads(buttons_json or "[]")
    except (ValueError, TypeError):
        buttons = []
    return ", ".join(b["text"] for b in buttons) if buttons else "нет"


def preview(text: str | None, limit: int = 60) -> str:
    if not text:
        return "без текста"
    flat = " ".join(text.split())
    return flat[:limit] + ("…" if len(flat) > limit else "")
