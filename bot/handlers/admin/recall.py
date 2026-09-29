"""🧯 Отзыв публикаций: удалить только что присланные посты и отменить ожидающие."""
from __future__ import annotations

import time

from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.handlers.admin.common import kb, screen_text, show
from bot.recall import build_plan, run_recall

router = Router(name="admin-recall")

HINT = "Удаляет у людей присланные ботом посты прогрева и рассылок. Работает 48 часов после отправки."
WINDOWS = [("15 минут", 900), ("1 час", 3600), ("3 часа", 10800), ("24 часа", 86400)]


def _window_title(seconds: int) -> str:
    return next((title for title, sec in WINDOWS if sec == seconds), f"{seconds // 60} мин")


@router.callback_query(F.data == "a:rec")
async def cb_recall(call: CallbackQuery, deps) -> None:
    rows = [[(f"За {title}", f"a:rec:w:{sec}")] for title, sec in WINDOWS]
    rows.append([("⬅️ К прогреву", "a:fun")])
    await show(
        call,
        screen_text("🧯 Отозвать отправленное", HINT, "За какой период удалить публикации?"),
        kb(rows),
    )
    await call.answer()


@router.callback_query(F.data.startswith("a:rec:w:"))
async def cb_recall_window(call: CallbackQuery, deps) -> None:
    seconds = int(call.data.split(":")[-1])
    plan = await build_plan(deps, int(time.time()) - seconds)
    if plan.is_empty:
        await show(
            call,
            screen_text("🧯 Отозвать отправленное", HINT, f"За {_window_title(seconds)} ничего не отправлялось."),
            kb([[("⬅️ Назад", "a:rec")]]),
        )
        await call.answer()
        return
    body = (
        f"За {_window_title(seconds)} получили публикации: <b>{len(plan.users)}</b> чел.\n"
        f"Удалю точно (по журналу): {plan.exact_total} сообщ.\n"
        f"Удалю по диапазону (отправлено до журнала): ~{plan.legacy_total} сообщ.\n\n"
        "Ещё не отправленные посты, срок которых уже наступил, будут отменены. "
        "Отправленные шаги этим людям больше не придут.\n\n"
        "⚠️ По диапазону бот удаляет последние сообщения в чате по расчёту — если человек "
        "успел что-то написать, оно тоже может удалиться."
    )
    await show(
        call,
        screen_text("🧯 Отозвать отправленное", "Проверьте и подтвердите.", body),
        kb([[("🗑 Удалить и отменить", f"a:rec:go:{seconds}")], [("⬅️ Назад", "a:rec")]]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("a:rec:go:"))
async def cb_recall_go(call: CallbackQuery, deps) -> None:
    seconds = int(call.data.split(":")[-1])
    await call.answer("Удаляю…")
    await show(call, "⏳ Удаляю публикации у людей, это может занять несколько минут…")
    result = await run_recall(call.bot, deps, int(time.time()) - seconds)
    await show(
        call,
        screen_text(
            "🧯 Готово",
            "Отзыв выполнен.",
            f"Людей обработано: {result['users']}\n"
            f"Удалено точно: {result['exact']}\n"
            f"Удалено по диапазону: {result['ranged']}\n"
            f"Заблокировали бота: {result['blocked']}\n"
            f"Не вышло: {result['failed']}\n"
            f"Отменено ожидающих постов: {result['cancelled']}",
        ),
        kb([[("⬅️ К прогреву", "a:fun")]]),
    )
