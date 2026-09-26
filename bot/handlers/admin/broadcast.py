"""Массовые рассылки: сбор сообщений, сегменты, отправка и планирование."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.content import ContentBlock
from bot.sender import send_block
from bot.handlers.admin.common import (
    BroadcastItemEdit,
    BroadcastNew,
    MenuRef,
    capture_content,
    kb,
    parse_buttons,
    preview,
    safe_excerpt,
    show,
)

log = logging.getLogger(__name__)
router = Router(name="admin-broadcast")

TZ = ZoneInfo(os.environ.get("TZ", "Europe/Moscow"))
_tasks: set[asyncio.Task] = set()

SEGMENTS = [
    ("all", "Все активные"),
    ("subscribed", "Подписаны на канал"),
    ("not_subscribed", "Не подписаны"),
    ("got_material", "Получили материал"),
    ("no_material", "Ещё не получили материал"),
]

HINT = (
    "📤 <b>Рассылка</b>\n\n"
    "Пришли боту одно или несколько сообщений — текст, фото, видео, кружок, файл, "
    "аудио, голосовое, гифка или стикер.\n\n"
    "Дальше сможешь посмотреть список, поправить текст/медиа, добавить кнопки-ссылки "
    "и только потом выбрать получателей."
)


def fmt_time(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M")


STATUS_LABELS = {
    "draft": "черновик",
    "queued": "запланирована",
    "running": "идёт",
    "done": "завершена",
    "cancelled": "отменена",
}


async def broadcast_screen(target, deps) -> None:
    recent = await deps.broadcasts.recent(limit=8)
    lines = []
    rows = []
    for row in recent:
        stats = await deps.broadcasts.stats(row["id"])
        when = fmt_time(row["scheduled_at"] or row["created_at"])
        status = STATUS_LABELS.get(row["status"], row["status"])
        lines.append(
            f"#{row['id']} {when} — {status}: отправлено {stats['sent']}, "
            f"заблокировали {stats['blocked']}, ошибок {stats['failed']}"
        )
        rows.append([(f"📨 #{row['id']} · {when} · {status}", f"a:bc:o:{row['id']}")])
    text = HINT + "\n\n<b>Последние рассылки</b> (нажми, чтобы открыть или удалить):\n" + (
        "\n".join(lines) if lines else "пока не было"
    )
    rows = [[("➕ Новая рассылка", "a:bc:new")]] + rows + [[("⬅️ Назад", "a:menu")]]
    await show(target, text, kb(rows))


async def broadcast_card(target, deps, broadcast_id: int) -> None:
    row = await deps.broadcasts.get(broadcast_id)
    if row is None:
        await broadcast_screen(target, deps)
        return
    stats = await deps.broadcasts.stats(broadcast_id)
    messages = await deps.broadcasts.messages(broadcast_id)
    label = dict(SEGMENTS).get(row["segment"], row["segment"])
    text = (
        f"📨 <b>Рассылка #{broadcast_id}</b> — {STATUS_LABELS.get(row['status'], row['status'])}\n\n"
        f"Сообщений: {len(messages)}\n"
        f"Получатели: {label} — {stats['total']}\n"
        f"Отправлено {stats['sent']}, заблокировали {stats['blocked']}, ошибок {stats['failed']}"
    )
    rows = [[("👁 Предпросмотр", f"a:bc:p:{broadcast_id}")]]
    if row["status"] in ("draft", "queued"):
        rows.append([("🚀 Отправить сейчас", f"a:bc:go:{broadcast_id}")])
        rows.append([("🕓 Запланировать", f"a:bc:sch:{broadcast_id}")])
    if row["status"] != "running":
        rows.append([("🗑 Удалить", f"a:bc:del:{broadcast_id}")])
    rows.append([("⬅️ К списку", "a:bc")])
    await show(target, text, kb(rows))


@router.callback_query(F.data.startswith("a:bc:o:"))
async def cb_open(call: CallbackQuery, deps) -> None:
    await broadcast_card(call, deps, int(call.data.split(":")[-1]))
    await call.answer()


@router.callback_query(F.data.startswith("a:bc:del:"))
async def cb_delete_ask(call: CallbackQuery, deps) -> None:
    broadcast_id = int(call.data.split(":")[-1])
    await show(
        call,
        f"🗑 Удалить рассылку #{broadcast_id}? Вернуть её будет нельзя.",
        kb([[("🗑 Да, удалить", f"a:bc:delok:{broadcast_id}")], [("✖️ Нет", f"a:bc:o:{broadcast_id}")]]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("a:bc:delok:"))
async def cb_delete_do(call: CallbackQuery, deps) -> None:
    broadcast_id = int(call.data.split(":")[-1])
    if await deps.broadcasts.delete(broadcast_id):
        await call.answer("Удалил")
    else:
        await call.answer("Идущую рассылку удалить нельзя", show_alert=True)
    await broadcast_screen(call, deps)


@router.callback_query(F.data == "a:bc")
async def cb_broadcast(call: CallbackQuery, deps, state: FSMContext) -> None:
    await state.clear()
    await broadcast_screen(call, deps)
    await call.answer()


@router.callback_query(F.data == "a:bc:new")
async def cb_new(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastNew.collecting)
    await state.update_data(
        messages=[], _menu_chat_id=call.message.chat.id, _menu_message_id=call.message.message_id
    )
    await show(
        call,
        "📤 <b>Сбор рассылки</b>\n\n"
        "Шли сообщения — можно несколько подряд.\n\n"
        "Когда закончишь, жми «✅ Готово».",
        kb([[("✅ Готово", "a:bc:done")], [("✖️ Отмена", "a:bc")]]),
    )
    await call.answer()


@router.message(BroadcastNew.collecting)
async def on_collect(message: Message, state: FSMContext, deps) -> None:
    """Копим сообщения для рассылки. Счётчик правим в одном и том же сообщении,
    чтобы при нескольких подряд присланных постах чат не зарастал одинаковыми уведомлениями."""
    text, media_id = await capture_content(deps, message, "broadcast")
    if not text and not media_id:
        await message.answer(
            "Такой тип сообщения пока не поддерживается для рассылки. Пришли текст, фото, "
            "видео, кружок, файл, аудио, голосовое, гифку или стикер."
        )
        return
    media_kind = file_id = None
    if media_id:
        media_row = await deps.media.get(media_id)
        if media_row:
            media_kind, file_id = media_row["kind"], media_row["file_id"]
    data = await state.get_data()
    messages = data.get("messages", [])
    messages.append({"text": text, "media_kind": media_kind, "file_id": file_id, "buttons": []})
    await state.update_data(messages=messages)
    text_out = f"📤 <b>Сбор рассылки</b>\n\nДобавлено сообщений: {len(messages)}\n\nШли ещё или жми «✅ Готово»."
    markup = kb([[("✅ Готово", "a:bc:done")], [("✖️ Отмена", "a:bc")]])
    chat_id, message_id = data.get("_menu_chat_id"), data.get("_menu_message_id")
    ref = MenuRef(message.bot, chat_id, message_id) if chat_id and message_id else None
    result = await show(ref or message, text_out, markup)
    if ref is None and result is not None:
        await state.update_data(_menu_chat_id=result.chat.id, _menu_message_id=result.message_id)


async def broadcast_draft_screen(target, state: FSMContext) -> None:
    """Список ещё не отправленных сообщений черновика — как экран блоков материала."""
    data = await state.get_data()
    messages = data.get("messages", [])
    rows = []
    lines = []
    for idx, item in enumerate(messages):
        media = f" [{item['media_kind']}]" if item.get("media_kind") else ""
        n_buttons = len(item.get("buttons") or [])
        btn_hint = f", кнопок: {n_buttons}" if n_buttons else ""
        lines.append(f"{idx + 1}. {preview(item.get('text'))}{media}{btn_hint}")
        rows.append(
            [
                (f"{idx + 1}. {preview(item.get('text'), 20)}", f"a:bc:d:{idx}"),
                ("🗑", f"a:bc:ddel:{idx}"),
            ]
        )
    rows.append([("➕ Добавить ещё", "a:bc:more")])
    if messages:
        rows.append([("▶️ Дальше — выбрать получателей", "a:bc:tosend")])
    rows.append([("✖️ Отмена", "a:bc")])
    body = "\n".join(lines) if lines else "Сообщений пока нет — пришли хотя бы одно."
    text = "📤 <b>Сбор рассылки</b>\n\n" + body
    await show(target, text, kb(rows))


async def broadcast_item_screen(target, state: FSMContext, idx: int) -> None:
    data = await state.get_data()
    messages = data.get("messages", [])
    if not (0 <= idx < len(messages)):
        await show(target, "Сообщение не найдено.", kb([[("⬅️ К списку", "a:bc:list")]]))
        return
    item = messages[idx]
    text = (
        f"📤 <b>Сообщение #{idx + 1}</b>\n\n"
        f"🎬 Медиа: {item.get('media_kind') or 'нет'}\n"
        f"🔘 Кнопки: {', '.join(b['text'] for b in item.get('buttons') or []) or 'нет'}\n\n"
        f"Текст:\n{safe_excerpt(item.get('text')) or '<i>без текста</i>'}"
    )
    await show(
        target,
        text,
        kb(
            [
                [("👁 Показать", f"a:bc:dprev:{idx}")],
                [("✏️ Текст/медиа", f"a:bc:dedit:{idx}"), ("🔘 Кнопки", f"a:bc:dbtn:{idx}")],
                [("⬆️ Выше", f"a:bc:dup:{idx}"), ("⬇️ Ниже", f"a:bc:ddn:{idx}")],
                [("🗑 Удалить", f"a:bc:ddel:{idx}")],
                [("⬅️ К списку", "a:bc:list")],
            ]
        ),
    )


@router.callback_query(F.data == "a:bc:done")
async def cb_done(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("messages"):
        await call.answer("Сначала пришли хотя бы одно сообщение", show_alert=True)
        return
    await broadcast_draft_screen(call, state)
    await call.answer()


@router.callback_query(F.data == "a:bc:list")
async def cb_draft_list(call: CallbackQuery, state: FSMContext) -> None:
    await broadcast_draft_screen(call, state)
    await call.answer()


@router.callback_query(F.data == "a:bc:more")
async def cb_draft_more(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastNew.collecting)
    await show(
        call,
        "📤 <b>Сбор рассылки</b>\n\nШли ещё сообщения — можно несколько подряд.\n\nКогда закончишь, жми «✅ Готово».",
        kb([[("✅ Готово", "a:bc:done")], [("✖️ Отмена", "a:bc")]]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("a:bc:d:"))
async def cb_draft_item(call: CallbackQuery, state: FSMContext) -> None:
    idx = int(call.data.split(":")[-1])
    await broadcast_item_screen(call, state, idx)
    await call.answer()


@router.callback_query(F.data.startswith("a:bc:dprev:"))
async def cb_draft_preview(call: CallbackQuery, state: FSMContext) -> None:
    idx = int(call.data.split(":")[-1])
    data = await state.get_data()
    messages = data.get("messages", [])
    if not (0 <= idx < len(messages)):
        await call.answer("Сообщение не найдено", show_alert=True)
        return
    await send_block(_block_from_item(messages[idx]), call.bot, call.message.chat.id, user=call.from_user)
    await call.answer("Это увидят люди")


@router.callback_query(F.data.startswith("a:bc:ddel:"))
async def cb_draft_delete(call: CallbackQuery, state: FSMContext) -> None:
    idx = int(call.data.split(":")[-1])
    data = await state.get_data()
    messages = data.get("messages", [])
    if 0 <= idx < len(messages):
        messages.pop(idx)
        await state.update_data(messages=messages)
    await call.answer("Удалил")
    await broadcast_draft_screen(call, state)


@router.callback_query(F.data.startswith("a:bc:dup:"))
async def cb_draft_up(call: CallbackQuery, state: FSMContext) -> None:
    await _move_draft_item(state, int(call.data.split(":")[-1]), -1)
    await call.answer()
    await broadcast_draft_screen(call, state)


@router.callback_query(F.data.startswith("a:bc:ddn:"))
async def cb_draft_down(call: CallbackQuery, state: FSMContext) -> None:
    await _move_draft_item(state, int(call.data.split(":")[-1]), 1)
    await call.answer()
    await broadcast_draft_screen(call, state)


async def _move_draft_item(state: FSMContext, idx: int, direction: int) -> None:
    data = await state.get_data()
    messages = data.get("messages", [])
    new_idx = idx + direction
    if 0 <= idx < len(messages) and 0 <= new_idx < len(messages):
        messages[idx], messages[new_idx] = messages[new_idx], messages[idx]
        await state.update_data(messages=messages)


@router.callback_query(F.data.startswith("a:bc:dedit:"))
async def cb_draft_edit(call: CallbackQuery, state: FSMContext) -> None:
    idx = int(call.data.split(":")[-1])
    await state.update_data(edit_idx=idx)
    await state.set_state(BroadcastItemEdit.waiting_content)
    await show(
        call,
        "✏️ <b>Новое содержимое сообщения</b>\n\nПришли одним сообщением — заменит и текст, и медиа целиком.",
        kb([[("⬅️ Отмена", f"a:bc:d:{idx}")]]),
    )
    await call.answer()


@router.message(BroadcastItemEdit.waiting_content)
async def on_draft_edit_content(message: Message, state: FSMContext, deps) -> None:
    data = await state.get_data()
    idx = data.get("edit_idx")
    messages = data.get("messages", [])
    if idx is None or not (0 <= idx < len(messages)):
        await state.set_state(BroadcastNew.collecting)
        return
    text, media_id = await capture_content(deps, message, "broadcast")
    if not text and not media_id:
        await message.answer("Пустое сообщение. Пришли текст или файл.")
        return
    media_kind = file_id = None
    if media_id:
        media_row = await deps.media.get(media_id)
        if media_row:
            media_kind, file_id = media_row["kind"], media_row["file_id"]
    messages[idx] = {
        "text": text,
        "media_kind": media_kind,
        "file_id": file_id,
        "buttons": messages[idx].get("buttons", []),
    }
    await state.update_data(messages=messages)
    await state.set_state(BroadcastNew.collecting)
    await broadcast_item_screen(message, state, idx)


@router.callback_query(F.data.startswith("a:bc:dbtn:"))
async def cb_draft_buttons(call: CallbackQuery, state: FSMContext) -> None:
    idx = int(call.data.split(":")[-1])
    await state.update_data(edit_idx=idx)
    await state.set_state(BroadcastItemEdit.waiting_buttons)
    await show(
        call,
        "🔘 <b>Кнопки сообщения</b>\n\nПришли построчно:\n<code>Текст кнопки | https://ссылка</code>\n\n"
        "Чтобы убрать все кнопки — отправь <code>-</code>",
        kb([[("⬅️ Отмена", f"a:bc:d:{idx}")]]),
    )
    await call.answer()


@router.message(BroadcastItemEdit.waiting_buttons, F.text)
async def on_draft_buttons(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    idx = data.get("edit_idx")
    messages = data.get("messages", [])
    if idx is None or not (0 <= idx < len(messages)):
        await state.set_state(BroadcastNew.collecting)
        return
    buttons = [] if message.text.strip() == "-" else parse_buttons(message.text)
    messages[idx]["buttons"] = buttons
    await state.update_data(messages=messages)
    await state.set_state(BroadcastNew.collecting)
    await broadcast_item_screen(message, state, idx)


@router.callback_query(F.data == "a:bc:tosend")
async def cb_pick_segment(call: CallbackQuery, deps, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("messages"):
        await call.answer("Сначала пришли хотя бы одно сообщение", show_alert=True)
        return
    rows = []
    for key, label in SEGMENTS:
        count = await deps.users.segment_count(key)
        rows.append([(f"{label} — {count}", f"a:bc:seg:{key}")])
    rows.append([("✖️ Отмена", "a:bc")])
    await show(call, "📤 <b>Кому отправляем?</b>\n\nВыбери сегмент получателей:", kb(rows))
    await call.answer()


@router.callback_query(F.data.startswith("a:bc:seg:"))
async def cb_segment(call: CallbackQuery, deps, state: FSMContext) -> None:
    segment = call.data.split(":")[-1]
    data = await state.get_data()
    messages = data.get("messages") or []
    if not messages:
        await call.answer("Черновик потерялся, начни заново", show_alert=True)
        return
    broadcast_id, total = await deps.engine.prepare(call.from_user.id, segment, messages)
    await state.clear()
    label = dict(SEGMENTS).get(segment, segment)
    await show(
        call,
        f"📤 <b>Рассылка #{broadcast_id} готова</b>\n\n"
        f"Сообщений: {len(messages)}\n"
        f"Получателей: <b>{total}</b> ({label})\n\n"
        f"⏱ Время отправки: ~{_eta(total, deps)}",
        kb(
            [
                [("👁 Предпросмотр", f"a:bc:p:{broadcast_id}")],
                [("🚀 Отправить сейчас", f"a:bc:go:{broadcast_id}")],
                [("🕓 Запланировать", f"a:bc:sch:{broadcast_id}")],
                [("🗑 Удалить", f"a:bc:del:{broadcast_id}")],
            ]
        ),
    )
    await call.answer()


def _block_from_item(item: dict) -> ContentBlock:
    return ContentBlock(
        text=item.get("text"),
        media_kind=item.get("media_kind"),
        file_id=item.get("file_id"),
        buttons=item.get("buttons") or [],
    )


def _eta(total: int, deps) -> str:
    rate = deps.config.messages_per_second or 20
    seconds = int(total / rate) + 1
    if seconds < 60:
        return f"{seconds} сек"
    return f"{seconds // 60} мин"


@router.callback_query(F.data.startswith("a:bc:p:"))
async def cb_preview(call: CallbackQuery, deps) -> None:
    broadcast_id = int(call.data.split(":")[-1])
    for msg in await deps.broadcasts.messages(broadcast_id):
        if "chat_id" in msg and "message_id" in msg:
            await call.bot.copy_message(
                chat_id=call.message.chat.id,
                from_chat_id=msg["chat_id"],
                message_id=msg["message_id"],
            )
        else:
            await send_block(_block_from_item(msg), call.bot, call.message.chat.id, user=call.from_user)
    await call.answer("Это увидят люди")


@router.callback_query(F.data.startswith("a:bc:x:"))
async def cb_cancel(call: CallbackQuery, deps) -> None:
    broadcast_id = int(call.data.split(":")[-1])
    await deps.broadcasts.cancel(broadcast_id)
    await call.answer("Отменил")
    await broadcast_screen(call, deps)


@router.callback_query(F.data.startswith("a:bc:go:"))
async def cb_go(call: CallbackQuery, deps) -> None:
    broadcast_id = int(call.data.split(":")[-1])
    status_message = await call.message.answer("🚀 Отправляю…")
    await call.answer()

    async def progress(stats: dict[str, int]) -> None:
        try:
            await status_message.edit_text(
                f"🚀 Отправляю…\n\n"
                f"Готово: {stats['sent']} из {stats['total']}\n"
                f"Заблокировали: {stats['blocked']}\n"
                f"Ошибок: {stats['failed']}"
            )
        except Exception:  # noqa: BLE001 — Telegram не даёт редактировать слишком часто
            pass

    async def runner() -> None:
        stats = await deps.engine.run(broadcast_id, progress=progress)
        try:
            await status_message.edit_text(
                f"✅ Рассылка #{broadcast_id} завершена\n\n"
                f"Доставлено: {stats['sent']}\n"
                f"Заблокировали бота: {stats['blocked']}\n"
                f"Ошибок: {stats['failed']}\n"
                f"Всего в базе: {stats['total']}"
            )
        except Exception:  # noqa: BLE001
            await call.message.answer(f"✅ Рассылка #{broadcast_id} завершена: {stats}")

    task = asyncio.create_task(runner(), name=f"broadcast-{broadcast_id}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@router.callback_query(F.data.startswith("a:bc:sch:"))
async def cb_schedule(call: CallbackQuery, state: FSMContext) -> None:
    broadcast_id = int(call.data.split(":")[-1])
    await state.set_state(BroadcastNew.waiting_schedule)
    await state.update_data(broadcast_id=broadcast_id)
    now = datetime.now(TZ).strftime("%d.%m %H:%M")
    await show(
        call,
        "🕓 <b>Когда отправить?</b>\n\n"
        f"Время московское, сейчас {now}.\n\n"
        "Форматы: <code>21:30</code>, <code>завтра 10:00</code>, <code>20.09 14:00</code>",
        kb([[("⬅️ Отмена", "a:bc")]]),
    )
    await call.answer()


@router.message(BroadcastNew.waiting_schedule, F.text)
async def on_schedule(message: Message, state: FSMContext, deps) -> None:
    when = parse_schedule(message.text)
    if when is None:
        await message.answer("Не понял время. Примеры: <code>21:30</code>, <code>завтра 10:00</code>, <code>20.09 14:00</code>")
        return
    data = await state.get_data()
    broadcast_id = data["broadcast_id"]
    await deps.db.execute(
        "UPDATE broadcasts SET scheduled_at = ?, status = 'queued' WHERE id = ?",
        (when, broadcast_id),
    )
    await state.clear()
    await broadcast_screen(message, deps)


def parse_schedule(raw: str, now: datetime | None = None) -> int | None:
    """«21:30», «завтра 10:00», «20.09 14:00» → unix-время."""
    text = (raw or "").strip().lower()
    now = now or datetime.now(TZ)

    match = re.match(r"^(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?\s+(\d{1,2}):(\d{2})$", text)
    if match:
        day, month, year, hour, minute = match.groups()
        year_int = int(year) if year else now.year
        if year_int < 100:
            year_int += 2000
        try:
            dt = datetime(year_int, int(month), int(day), int(hour), int(minute), tzinfo=TZ)
        except ValueError:
            return None
        return int(dt.timestamp())

    tomorrow = text.startswith("завтра")
    text = text.replace("завтра", "").replace("сегодня", "").strip()
    match = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if tomorrow or dt <= now:
        dt += timedelta(days=1)
    return int(dt.timestamp())
