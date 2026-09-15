"""Массовые рассылки: сбор сообщений, сегменты, отправка и планирование."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.handlers.admin.common import BroadcastNew, kb, show

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
    "Пришли боту сообщения — текст, фото, видео, кружок, файл, хоть несколько подряд.\n"
    "Они уйдут людям ровно в том виде, в каком ты их отправил."
)


def fmt_time(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M")


async def broadcast_screen(target, deps) -> None:
    recent = await deps.broadcasts.recent(limit=5)
    lines = []
    for row in recent:
        stats = await deps.broadcasts.stats(row["id"])
        when = fmt_time(row["scheduled_at"] or row["created_at"])
        lines.append(
            f"#{row['id']} {when} — {row['status']}: отправлено {stats['sent']}, "
            f"заблокировали {stats['blocked']}, ошибок {stats['failed']}"
        )
    text = HINT + "\n\n<b>Последние рассылки:</b>\n" + ("\n".join(lines) if lines else "пока не было")
    await show(target, text, kb([[("➕ Новая рассылка", "a:bc:new")], [("⬅️ Назад", "a:menu")]]))


@router.callback_query(F.data == "a:bc")
async def cb_broadcast(call: CallbackQuery, deps, state: FSMContext) -> None:
    await state.clear()
    await broadcast_screen(call, deps)
    await call.answer()


@router.callback_query(F.data == "a:bc:new")
async def cb_new(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastNew.collecting)
    await state.update_data(messages=[])
    await show(
        call,
        "Шли сообщения для рассылки — можно несколько подряд.\n"
        "Когда закончишь, жми «Готово».",
        kb([[("✅ Готово", "a:bc:done")], [("✖️ Отмена", "a:bc")]]),
    )
    await call.answer()


@router.message(BroadcastNew.collecting)
async def on_collect(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    messages = data.get("messages", [])
    messages.append({"chat_id": message.chat.id, "message_id": message.message_id})
    await state.update_data(messages=messages)
    await message.answer(
        f"Добавлено сообщений: {len(messages)}",
        reply_markup=kb([[("✅ Готово", "a:bc:done")], [("✖️ Отмена", "a:bc")]]),
    )


@router.callback_query(F.data == "a:bc:done")
async def cb_done(call: CallbackQuery, deps, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("messages"):
        await call.answer("Сначала пришли хотя бы одно сообщение", show_alert=True)
        return
    rows = []
    for key, label in SEGMENTS:
        count = await deps.users.segment_count(key)
        rows.append([(f"{label} — {count}", f"a:bc:seg:{key}")])
    rows.append([("✖️ Отмена", "a:bc")])
    await show(call, "Кому отправляем?", kb(rows))
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
        f"Рассылка #{broadcast_id} готова.\n\n"
        f"Сообщений: {len(messages)}\n"
        f"Получателей: <b>{total}</b> ({label})\n\n"
        f"Примерное время отправки: {_eta(total, deps)}",
        kb(
            [
                [("👁 Предпросмотр", f"a:bc:p:{broadcast_id}")],
                [("🚀 Отправить сейчас", f"a:bc:go:{broadcast_id}")],
                [("🕓 Запланировать", f"a:bc:sch:{broadcast_id}")],
                [("✖️ Отменить", f"a:bc:x:{broadcast_id}")],
            ]
        ),
    )
    await call.answer()


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
        await call.bot.copy_message(
            chat_id=call.message.chat.id,
            from_chat_id=msg["chat_id"],
            message_id=msg["message_id"],
        )
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
        f"Когда отправить? Время московское, сейчас {now}.\n\n"
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
    await message.answer(f"Запланировал на {fmt_time(when)} ✅\nОтправится само, бот трогать не нужно.")
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
