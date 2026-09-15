"""Единица контента: текст + медиа + кнопки. Умеет превращаться в вызовы Telegram."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Типы, у которых нет подписи — текст придётся слать отдельным сообщением
CAPTIONLESS = {"video_note", "sticker"}
CAPTION_LIMIT = 1024

Factory = Callable[[], Awaitable[Any]]


@dataclass
class ContentBlock:
    text: str | None = None
    media_kind: str | None = None
    file_id: str | None = None
    buttons: list[dict] = field(default_factory=list)

    @classmethod
    def from_rows(cls, row: Any, media_row: Any = None) -> "ContentBlock":
        buttons = []
        raw = row["buttons_json"] if "buttons_json" in row.keys() else None
        if raw:
            try:
                buttons = json.loads(raw)
            except (ValueError, TypeError):
                buttons = []
        return cls(
            text=row["text"],
            media_kind=media_row["kind"] if media_row else None,
            file_id=media_row["file_id"] if media_row else None,
            buttons=buttons,
        )

    @property
    def is_empty(self) -> bool:
        return not self.text and not self.file_id

    def keyboard(self) -> InlineKeyboardMarkup | None:
        rows = []
        for btn in self.buttons:
            text = btn.get("text")
            url = btn.get("url")
            if text and url:
                rows.append([InlineKeyboardButton(text=text, url=url)])
        return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

    def render(self, user: Any = None) -> str | None:
        """Подставляет {name} и {username} в текст."""
        if not self.text:
            return None
        name = ""
        username = ""
        if user is not None:
            try:
                name = user["first_name"] or ""
                username = user["username"] or ""
            except (TypeError, KeyError, IndexError):
                name = getattr(user, "first_name", "") or ""
                username = getattr(user, "username", "") or ""
        return self.text.replace("{name}", name).replace("{username}", username)

    def factories(self, bot: Bot, chat_id: int, user: Any = None) -> list[Factory]:
        """Список отправок: обычно одна, для кружка с текстом — две."""
        text = self.render(user)
        kb = self.keyboard()
        out: list[Factory] = []

        if self.file_id and self.media_kind:
            kind = self.media_kind
            send = _SENDERS.get(kind)
            if send is None:
                raise ValueError(f"Неизвестный тип медиа: {kind}")
            caption_ok = kind not in CAPTIONLESS and text and len(text) <= CAPTION_LIMIT
            caption = text if caption_ok else None
            out.append(_bind(send, bot, chat_id, self.file_id, caption, kb))
            if text and not caption_ok:
                out.append(_bind(_send_text, bot, chat_id, None, text, kb))
        elif text:
            out.append(_bind(_send_text, bot, chat_id, None, text, kb))
        return out


def _bind(func, bot, chat_id, file_id, caption, kb) -> Factory:
    async def call():
        return await func(bot, chat_id, file_id, caption, kb)

    return call


async def _send_text(bot: Bot, chat_id: int, _file_id, caption, kb):
    return await bot.send_message(chat_id, caption, reply_markup=kb)


async def _send_photo(bot, chat_id, file_id, caption, kb):
    return await bot.send_photo(chat_id, file_id, caption=caption, reply_markup=kb)


async def _send_video(bot, chat_id, file_id, caption, kb):
    return await bot.send_video(chat_id, file_id, caption=caption, reply_markup=kb)


async def _send_video_note(bot, chat_id, file_id, _caption, kb):
    return await bot.send_video_note(chat_id, file_id, reply_markup=kb)


async def _send_document(bot, chat_id, file_id, caption, kb):
    return await bot.send_document(chat_id, file_id, caption=caption, reply_markup=kb)


async def _send_audio(bot, chat_id, file_id, caption, kb):
    return await bot.send_audio(chat_id, file_id, caption=caption, reply_markup=kb)


async def _send_voice(bot, chat_id, file_id, caption, kb):
    return await bot.send_voice(chat_id, file_id, caption=caption, reply_markup=kb)


async def _send_animation(bot, chat_id, file_id, caption, kb):
    return await bot.send_animation(chat_id, file_id, caption=caption, reply_markup=kb)


async def _send_sticker(bot, chat_id, file_id, _caption, kb):
    return await bot.send_sticker(chat_id, file_id, reply_markup=kb)


_SENDERS = {
    "photo": _send_photo,
    "video": _send_video,
    "video_note": _send_video_note,
    "document": _send_document,
    "audio": _send_audio,
    "voice": _send_voice,
    "animation": _send_animation,
    "sticker": _send_sticker,
}


def apply_placeholders(text: str | None, channel_url: str = "") -> str | None:
    """{channel} → ссылка на канал (Telegram сам покажет превью канала)."""
    if not text:
        return text
    return text.replace("{channel}", channel_url or "")


def extract_media(message: Any) -> tuple[str, str, str | None] | None:
    """Достать (kind, file_id, file_unique_id) из сообщения Telegram."""
    if getattr(message, "photo", None):
        photo = message.photo[-1]
        return "photo", photo.file_id, photo.file_unique_id
    for kind in ("video_note", "video", "animation", "document", "audio", "voice", "sticker"):
        obj = getattr(message, kind, None)
        if obj:
            return kind, obj.file_id, getattr(obj, "file_unique_id", None)
    return None
