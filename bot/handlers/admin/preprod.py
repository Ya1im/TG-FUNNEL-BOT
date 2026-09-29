"""🧪 Предпрод: публикации идут только на тестовые аккаунты — можно проверять тайминг и качество."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.handlers.admin.common import PreprodAdd, kb, require_owner, screen_text, show
from bot.preprod import is_enabled, parse_ids

router = Router(name="admin-preprod")

HINT = (
    "Пока предпрод включён, посты прогрева, рассылки и автовыдача урока идут только на тестовые "
    "аккаунты. Остальные люди ничего не получают."
)
MSK = timezone(timedelta(hours=3))
STATUS_ICON = {"pending": "⏳", "sent": "✅", "skipped": "⏭", "failed": "❌"}


async def preprod_screen(target, deps) -> None:
    on = await is_enabled(deps.settings)
    ids = parse_ids(await deps.settings.get("preprod_user_ids"))
    body = (
        f"Режим: {'🟡 ВКЛЮЧЁН — публикации только тестовым аккаунтам' if on else '🟢 выключен — публикации идут всем'}\n"
        f"Тестовые аккаунты: {', '.join(f'<code>{i}</code>' for i in ids) if ids else 'не добавлены'}\n"
        f"Админы всегда получают публикации.\n\n"
        "Как проверить: добавьте второй аккаунт (его ID покажет команда /myid), откройте им бота, "
        "нажмите /start и пройдите воронку. «Расписание» покажет, когда что придёт."
    )
    rows = [
        [("🟢 Выпустить в продакшен" if on else "🟡 Включить предпрод", "a:pre:tgl")],
        [("➕ Добавить тестовый аккаунт", "a:pre:add"), ("🗑 Очистить список", "a:pre:clr")],
        [("📅 Расписание тестовых аккаунтов", "a:pre:sch")],
        [("⬅️ К прогреву", "a:fun")],
    ]
    await show(target, screen_text("🧪 Предпрод", HINT, body), kb(rows))


@router.callback_query(F.data == "a:pre")
async def cb_preprod(call: CallbackQuery, deps) -> None:
    await preprod_screen(call, deps)
    await call.answer()


@router.callback_query(F.data == "a:pre:tgl")
async def cb_preprod_toggle(call: CallbackQuery, deps) -> None:
    if not await require_owner(call, deps):
        return
    if await is_enabled(deps.settings):
        await show(
            call,
            screen_text(
                "🟢 Выпустить в продакшен?",
                "Публикации пойдут всем.",
                "Ожидающие посты, срок которых уже наступил, уйдут ВСЕМ сразу. "
                "Если вы только что вносили шаги в бота — сначала проверьте очередь.",
            ),
            kb([[("✅ Да, выпустить", "a:pre:off")], [("⬅️ Отмена", "a:pre")]]),
        )
    else:
        await deps.settings.set("preprod_mode", "1")
        await preprod_screen(call, deps)
    await call.answer()


@router.callback_query(F.data == "a:pre:off")
async def cb_preprod_off(call: CallbackQuery, deps) -> None:
    if not await require_owner(call, deps):
        return
    await deps.settings.set("preprod_mode", "0")
    await preprod_screen(call, deps)
    await call.answer("Предпрод выключен")


@router.callback_query(F.data == "a:pre:add")
async def cb_preprod_add(call: CallbackQuery, deps, state: FSMContext) -> None:
    if not await require_owner(call, deps):
        return
    await state.set_state(PreprodAdd.waiting_value)
    await show(
        call,
        "➕ <b>Тестовый аккаунт</b>\n\nПришлите числовой ID аккаунта (его покажет команда /myid, "
        "которую нужно отправить боту с этого аккаунта) или перешлите сюда любое сообщение оттуда.",
        kb([[("⬅️ Отмена", "a:pre")]]),
    )
    await call.answer()


@router.message(PreprodAdd.waiting_value)
async def on_preprod_add(message: Message, state: FSMContext, deps) -> None:
    found: list[int] = []
    origin = getattr(message, "forward_from", None)
    if origin is not None:
        found = [origin.id]
    elif message.text:
        found = parse_ids(message.text)
    if not found:
        await message.answer("Не нашёл ID. Пришлите число или перешлите сообщение с этого аккаунта.")
        return
    current = parse_ids(await deps.settings.get("preprod_user_ids"))
    merged = current + [i for i in found if i not in current]
    await deps.settings.set("preprod_user_ids", ",".join(str(i) for i in merged))
    await state.clear()
    await preprod_screen(message, deps)


@router.callback_query(F.data == "a:pre:clr")
async def cb_preprod_clear(call: CallbackQuery, deps) -> None:
    if not await require_owner(call, deps):
        return
    await deps.settings.set("preprod_user_ids", "")
    await preprod_screen(call, deps)
    await call.answer("Список очищен")


@router.callback_query(F.data == "a:pre:sch")
async def cb_preprod_schedule(call: CallbackQuery, deps) -> None:
    ids = parse_ids(await deps.settings.get("preprod_user_ids"))
    lines: list[str] = []
    for uid in ids:
        rows = await deps.db.fetchall(
            "SELECT fs.position, us.due_at, us.status, us.sent_at, us.last_error "
            "FROM user_steps us JOIN funnel_steps fs ON fs.id = us.step_id "
            "WHERE us.user_id = ? ORDER BY fs.position, fs.id",
            (uid,),
        )
        lines.append(f"<b>{uid}</b>" + ("" if rows else " — очереди нет (пройдите /start и получите урок)"))
        for row in rows[:40]:
            when = datetime.fromtimestamp(row["sent_at"] if row["sent_at"] else row["due_at"], MSK)
            note = f" ({row['last_error']})" if row["last_error"] else ""
            lines.append(
                f"{STATUS_ICON.get(row['status'], '•')} шаг {row['position']} — "
                f"{'отправлен' if row['status'] == 'sent' else 'по плану'} {when:%d.%m %H:%M} МСК{note}"
            )
    body = "\n".join(lines) if lines else "Тестовые аккаунты не добавлены."
    await show(call, screen_text("📅 Расписание (МСК)", "Что и когда придёт тестовым аккаунтам.", body), kb([[("⬅️ Назад", "a:pre")]]))
    await call.answer()
