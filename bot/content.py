"""Единица контента: текст + медиа + кнопки. Умеет превращаться в вызовы Telegram."""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from aiogram import Bot
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)

# Типы, у которых нет подписи — текст придётся слать отдельным сообщением
CAPTIONLESS = {"video_note", "sticker"}
CAPTION_LIMIT = 1024
PREMIUM_CAPTION_LIMIT = 2048   # у Premium-аккаунта заказчика подпись может быть до 2048
_caption_limit = PREMIUM_CAPTION_LIMIT   # оптимистично; если Telegram откажет — понижаем до 1024
_TAG_RE = re.compile(r"<[^>]*>")


_custom_emoji_ok: bool | None = None   # None — ещё не знаем; берётся из ответов Telegram на реальные отправки и проверки


def get_custom_emoji_status() -> bool | None:
    return _custom_emoji_ok


def set_custom_emoji_status(value: bool | None) -> None:
    global _custom_emoji_ok
    _custom_emoji_ok = value


def get_caption_limit() -> int:
    return _caption_limit


def set_caption_limit(value: int) -> None:
    global _caption_limit
    _caption_limit = int(value)


_copy_long_ok: bool | None = None   # None — не знаем; False — Telegram отказал копировать пост с такой подписью


def get_copy_status() -> bool | None:
    return _copy_long_ok


def set_copy_status(value: bool | None) -> None:
    global _copy_long_ok
    _copy_long_ok = value


def visible_len(text: str | None) -> int:
    """Длина видимого текста: без HTML-тегов и с раскрытыми сущностями. Именно её считает Telegram
    для лимита подписи; сама HTML-строка длиннее (каждое кастомное эмодзи — ещё ~50 служебных символов)."""
    if not text:
        return 0
    return len(html.unescape(_TAG_RE.sub("", text)))

Factory = Callable[[], Awaitable[Any]]

@dataclass
class ContentBlock:
    text: str | None = None
    media_kind: str | None = None
    file_id: str | None = None
    buttons: list[dict] = field(default_factory=list)
    # Альбом: [{"kind": "photo|video|document|audio", "file_id": ...}, ...] — уходит одним сообщением-группой.
    # Telegram не даёт вешать кнопки на альбом, поэтому buttons для него игнорируются.
    album: list[dict] | None = None
    # Где лежит исходное сообщение админа (чат и номера сообщений; у альбома их несколько). Нужно для
    # длинной подписи: Telegram не даёт боту отправить её сам, но копия исходного сообщения уходит как есть.
    source_chat: int | None = None
    source_ids: list[int] | None = None

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
        return not self.text and not self.file_id and not self.album

    def _captionable(self) -> bool:
        """Есть медиа, к которому можно приложить подпись (альбом — подпись на первом файле)."""
        if self.album:
            return True
        return bool(self.file_id and self.media_kind and self.media_kind not in CAPTIONLESS)

    def keyboard(self) -> InlineKeyboardMarkup | None:
        rows = []
        for btn in self.buttons:
            text = btn.get("text")
            url = btn.get("url")
            if not (text and url):
                continue
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

    def fallback_factory(self, bot: Bot, chat_id: int, user: Any = None) -> Factory | None:
        """Запасной вариант, если основной формат отклонён приватностью получателя
        (см. VOICE_FORBIDDEN_FALLBACK) — сейчас только кружок → обычное видео.

        Без подписи: кружок — captionless-тип, поэтому текст (если есть) уже
        запланирован отдельным сообщением в factories() — не дублируем его тут.
        """
        send = VOICE_FORBIDDEN_FALLBACK.get(self.media_kind)
        if not send or not self.file_id:
            return None
        # текст пойдёт отдельным сообщением — кнопка будет под ним, а не под видео (иначе кнопки две)
        kb = None if self.render(user) else self.keyboard()
        return _bind(send, bot, chat_id, self.file_id, None, kb)

    def can_copy(self, user: Any = None) -> bool:
        """Можно отправить копией исходного сообщения: оно известно, а текст не требует подстановок ({name})."""
        if not (self.source_chat and self.source_ids):
            return False
        text = self.text or ""
        return "{name}" not in text and "{username}" not in text

    def copy_factory(self, bot: Bot, chat_id: int, user: Any = None) -> Factory | None:
        """Копия исходного сообщения админа: текст, форматирование и эмодзи ровно такие, как он прислал.
        Кнопки добавляются (у альбома их нет — Telegram не позволяет)."""
        if not self.can_copy(user):
            return None
        source_chat, ids = self.source_chat, list(self.source_ids)

        async def call():
            if len(ids) > 1 or self.album:
                return await bot.copy_messages(chat_id, source_chat, ids)
            return await bot.copy_message(chat_id, source_chat, ids[0], reply_markup=self.keyboard())

        return call

    def caption_overflow(self, user: Any = None) -> bool:
        """Текст с медиа не помещается в подпись (по известному сейчас лимиту) и уйдёт вторым сообщением."""
        text = self.render(user)
        return bool(self._captionable() and text and visible_len(text) > get_caption_limit())

    def long_caption(self, user: Any = None) -> bool:
        """Подпись длиннее обычного лимита 1024, но уходит одним сообщением (расчёт на Premium-лимит)."""
        text = self.render(user)
        return bool(self._captionable() and text and CAPTION_LIMIT < visible_len(text) <= get_caption_limit())

    def factories(self, bot: Bot, chat_id: int, user: Any = None) -> list[Factory]:
        """Список отправок: обычно одна, для кружка с текстом — две."""
        text = self.render(user)
        kb = self.keyboard()
        out: list[Factory] = []

        if self.album:
            caption_ok = bool(text) and visible_len(text) <= get_caption_limit()
            out.append(_bind_album(self.album, bot, chat_id, text if caption_ok else None))
            if text and not caption_ok:  # подпись не влезла — текст уходит следом отдельным сообщением
                out.append(_bind(_send_text, bot, chat_id, None, text, kb))
        elif self.file_id and self.media_kind:
            kind = self.media_kind
            send = _SENDERS.get(kind)
            if send is None:
                raise ValueError(f"Неизвестный тип медиа: {kind}")
            caption_ok = kind not in CAPTIONLESS and text and visible_len(text) <= get_caption_limit()
            caption = text if caption_ok else None
            split = bool(text) and not caption_ok  # текст уходит отдельным сообщением
            # кнопка всегда одна: если есть отдельный текст — она под ним, а не под медиа
            out.append(_bind(send, bot, chat_id, self.file_id, caption, None if split else kb))
            if split:
                out.append(_bind(_send_text, bot, chat_id, None, text, kb))
        elif text:
            out.append(_bind(_send_text, bot, chat_id, None, text, kb))
        return out


def source_of(item: dict) -> tuple[int | None, list[int] | None]:
    """Откуда копировать пункт черновика рассылки: (чат админа, номера его исходных сообщений).
    У пункта, который админ исправил, ссылки нет — копия показала бы старое содержимое."""
    chat = item.get("src_chat")
    if not chat:
        return None, None
    if item.get("album"):
        return chat, sorted(part["message_id"] for part in item["album"])
    src = item.get("src_id")
    return (chat, [src]) if src else (None, None)


def _bind(func, bot, chat_id, file_id, caption, kb) -> Factory:
    async def call():
        return await func(bot, chat_id, file_id, caption, kb)

    return call


# Что Telegram умеет собирать в альбом (документы и аудио — только с такими же)
ALBUM_KINDS = {
    "photo": InputMediaPhoto,
    "video": InputMediaVideo,
    "document": InputMediaDocument,
    "audio": InputMediaAudio,
}


def _bind_album(album: list[dict], bot, chat_id, caption) -> Factory:
    async def call():
        media = [
            ALBUM_KINDS[item["kind"]](media=item["file_id"], caption=caption if index == 0 else None)
            for index, item in enumerate(album)
        ]
        return await bot.send_media_group(chat_id, media)

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

# Telegram под капотом использует один и тот же код ошибки VOICE_MESSAGES_FORBIDDEN
# и для голосовых, и для кружков (видеосообщений) — у пользователя это единая настройка
# приватности «Голосовые и видеосообщения». Кружок, отклонённый по этой причине,
# можно донести запасным способом — обычным (не круглым) видео тем же file_id.
VOICE_FORBIDDEN_FALLBACK = {"video_note": _send_video}


def is_voice_forbidden(error: str | None) -> bool:
    return bool(error) and "VOICE_MESSAGES_FORBIDDEN" in error


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
