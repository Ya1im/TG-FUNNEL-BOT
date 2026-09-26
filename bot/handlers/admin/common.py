"""Общее для админки: фильтр, навигация, разбор контента и кнопок."""
from __future__ import annotations

import html as html_lib
import json
import logging
import re
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

log = logging.getLogger(__name__)

BACK = "⬅️ Назад"

_TAG_RE = re.compile(r"<[^>]+>")

FALLBACK_ERROR_TEXT = (
    "⚠️ Не получилось показать этот экран — текст оказался слишком длинным "
    "или в нём символы, которые Telegram не принял. Ничего не потерялось, "
    "данные сохранены как есть. Открой раздел ещё раз; если это повторится "
    "на одном и том же пункте — сократи или упрости в нём текст."
)


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


class FunnelEditContent(StatesGroup):
    """Правка уже существующего шага прогрева — текст/медиа или кнопки отдельно."""

    waiting_content = State()
    waiting_buttons = State()


class FunnelImport(StatesGroup):
    waiting_file = State()


class MaterialAdd(StatesGroup):
    waiting_content = State()
    waiting_buttons = State()


class MaterialEdit(StatesGroup):
    """Правка уже существующего блока материала — текст/медиа или кнопки отдельно."""

    waiting_content = State()
    waiting_buttons = State()


class RepeatStartAdd(StatesGroup):
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


class BroadcastItemEdit(StatesGroup):
    """Правка одного ещё не отправленного сообщения черновика рассылки."""

    waiting_content = State()
    waiting_buttons = State()


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
    """Показать экран: правим сообщение меню, если оно есть, иначе шлём новое.

    Что бы ни случилось при отрисовке (например, текст оказался длиннее лимита
    Telegram или в нём затесались символы, ломающие HTML-разметку), эта функция
    не должна вылететь исключением наружу: если она это сделает, вызывающий
    хендлер не дойдёт до `call.answer()`, и кнопка в Telegram будет вечно
    крутиться, как будто бот завис. Поэтому при неудаче мы логируем причину
    и показываем короткое запасное сообщение вместо падения.
    """
    if isinstance(target, CallbackQuery):
        bot, chat_id, message_id = target.message.bot, target.message.chat.id, target.message.message_id
    elif isinstance(target, MenuRef):
        bot, chat_id, message_id = target.bot, target.chat_id, target.message_id
    else:
        try:
            return await target.answer(text, reply_markup=markup, disable_web_page_preview=True)
        except Exception:  # noqa: BLE001 — не даём битому тексту оставить чат без ответа
            log.exception("show(): не смог отправить сообщение с экраном")
            try:
                return await target.answer(FALLBACK_ERROR_TEXT, reply_markup=markup)
            except Exception:  # noqa: BLE001
                log.exception("show(): не смог отправить даже запасное сообщение")
                return None

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
    try:
        return await bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)
    except Exception:  # noqa: BLE001 — тот же битый/слишком длинный текст добьёт и это сообщение
        log.exception("show(): не смог отрисовать экран, показываю запасное сообщение")
        try:
            return await bot.send_message(chat_id, FALLBACK_ERROR_TEXT, reply_markup=markup)
        except Exception:  # noqa: BLE001
            log.exception("show(): не смог отправить даже запасное сообщение")
            return None


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


KIND_ICONS = {
    "text": "📝",
    "photo": "🖼",
    "video": "🎬",
    "video_note": "⭕",
    "document": "📄",
    "audio": "🎵",
    "voice": "🎙",
    "animation": "🎞",
    "sticker": "😀",
}
KIND_NAMES = {
    "photo": "фото",
    "video": "видео",
    "video_note": "кружок",
    "document": "файл",
    "audio": "аудио",
    "voice": "голосовое",
    "animation": "гифка",
    "sticker": "стикер",
}


def item_label(text: str | None, media_kind: str | None, limit: int = 40) -> str:
    """Одна строка про сообщение: иконка типа + короткое превью.

    Текст без медиа — 📝, с медиа — иконка медиа; медиа без подписи называем по типу."""
    icon = KIND_ICONS.get(media_kind or "text", "📝")
    body = preview(text, limit)
    if body == "без текста":
        body = KIND_NAMES.get(media_kind or "", "пусто")
    return f"{icon} {body}"


def screen_text(title: str, hint: str, body: str = "") -> str:
    """Короткий экран: жирный заголовок, подсказка курсивом, дальше список."""
    parts = [f"<b>{title}</b>", f"<i>{hint}</i>"]
    text = "\n".join(parts)
    return f"{text}\n\n{body}" if body else text


def preview(text: str | None, limit: int = 60) -> str:
    """Короткое превью текста для списков и подписей кнопок.

    Текст хранится в БД в HTML-разметке (жирный, ссылки — как их отдаёт
    Telegram). Раньше здесь резали эту HTML-строку по количеству символов
    "как есть": на длинном или отформатированном тексте обрезка нередко
    попадала внутрь тега (например, середина `<a href="...">`) или сущности
    (`&amp;`), и получившийся HTML был битым. Telegram отказывался показать
    такой экран ("can't parse entities"), а бот из-за этого не успевал
    ответить на нажатие — кнопка крутилась и ничего не происходило.
    Теперь сначала снимаем разметку и получаем чистый текст, режем уже его
    и лишь потом (если нужно) экранируем спецсимволы — так результат всегда
    валиден, сколько бы тегов или ссылок ни было в оригинале.
    """
    if not text:
        return "без текста"
    plain = html_lib.unescape(_TAG_RE.sub("", text))
    flat = " ".join(plain.split())
    if not flat:
        return "без текста"
    cut = flat[:limit]
    suffix = "…" if len(flat) > limit else ""
    return html_lib.escape(cut) + suffix


def safe_excerpt(text: str | None, limit: int = 3000) -> str:
    """Текст «как есть» для экранов вида «Сейчас: ...» / карточка блока.

    Короткий текст показываем целиком, с исходным форматированием — риска
    нет. Длинный (ближе к лимиту сообщения Telegram в 4096 символов, плюс
    заголовок экрана) обрезать так же наивно, как раньше делал preview(),
    опасно: можно разорвать тег или сущность и сломать весь экран (см.
    preview() выше) — или просто не влезть в лимит и получить
    `MESSAGE_TOO_LONG`. Поэтому для длинного текста снимаем разметку и
    показываем чистый обрезанный фрагмент с пометкой: обрезано только тут,
    в БД и при отправке пользователю текст остаётся полным.
    """
    if not text:
        return ""
    if len(text) <= limit:
        return text
    plain = html_lib.unescape(_TAG_RE.sub("", text))
    flat = " ".join(plain.split())
    return (
        html_lib.escape(flat[:limit]) + "…\n\n"
        "<i>Текст длиннее лимита показа — тут обрезан для примера, "
        "пользователю уйдёт полностью и с форматированием.</i>"
    )
