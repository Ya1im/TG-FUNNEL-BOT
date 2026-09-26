"""Статистика, таблица-отчёт (.xlsx) и обнуление базы."""
from __future__ import annotations

from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

from bot.handlers.admin.common import ReportChatSet, kb, show
from bot.stats_export import export_path, make_export

router = Router(name="admin-stats")


async def stats_screen(target, deps) -> None:
    stats = await deps.users.stats()
    sources = await deps.users.sources()
    pending = await deps.funnel.pending_count()
    report_chat = (await deps.settings.get("report_chat_id")).strip() or "не задан"
    interval = await deps.settings.get_int("stats_export_interval_min") or 60
    lines = [f"• {src}: {cnt}" for src, cnt in sources]
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Всего: {stats['total']}\n"
        f"✅ Активных: {stats['active']}\n"
        f"🚫 Заблокировали бота: {stats['blocked']}\n\n"
        f"📢 Подписаны на канал: {stats['subscribed']}\n"
        f"🎁 Получили материал: {stats['got_material']}\n"
        f"⏳ В очереди прогрева: {pending}\n\n"
        f"🆕 Пришли за сутки: {stats['today']}\n\n"
        "<b>Источники:</b>\n" + ("\n".join(lines) if lines else "нет данных") + "\n\n"
        f"📑 Таблица пересобирается каждые {interval} мин.\n"
        f"📤 Чат для пересылки: <code>{report_chat}</code>"
    )
    await show(
        target,
        text,
        kb(
            [
                [("📥 Скачать таблицу", "a:stat:file")],
                [("📤 Переслать эксперту", "a:stat:send")],
                [("✏️ Чат для пересылки", "a:stat:chat")],
                [("🧹 Обнулить статистику", "a:stat:reset")],
                [("🔄 Обновить", "a:stat")],
                [("⬅️ Назад", "a:menu")],
            ]
        ),
    )


async def _ensure_file(deps) -> Path:
    """Отдать уже собранный файл, а если планировщик ещё не успел — собрать сейчас."""
    path = export_path(deps.db)
    if not path.exists():
        path = await make_export(deps)
    return path


@router.callback_query(F.data == "a:stat")
async def cb_stats(call: CallbackQuery, deps) -> None:
    await stats_screen(call, deps)
    await call.answer()


@router.callback_query(F.data == "a:stat:file")
async def cb_stats_file(call: CallbackQuery, deps) -> None:
    path = await _ensure_file(deps)
    await call.message.answer_document(
        FSInputFile(path, filename="stats.xlsx"),
        caption="Таблица статистики.",
    )
    await call.answer()


@router.callback_query(F.data == "a:stat:send")
async def cb_stats_send(call: CallbackQuery, deps) -> None:
    chat_raw = (await deps.settings.get("report_chat_id")).strip()
    if not chat_raw:
        await call.answer(
            "Чат для пересылки не задан — сначала нажми «✏️ Чат для пересылки».",
            show_alert=True,
        )
        return
    chat_id = int(chat_raw) if chat_raw.lstrip("-").isdigit() else chat_raw
    path = await _ensure_file(deps)
    try:
        await call.bot.send_document(
            chat_id,
            FSInputFile(path, filename="stats.xlsx"),
            caption="Таблица статистики.",
        )
    except Exception as exc:  # noqa: BLE001 — показываем причину прямо админу
        await call.answer(f"Не смог отправить: {exc}", show_alert=True)
        return
    await call.answer("Переслал ✅")


@router.callback_query(F.data.in_({"a:stat:chat", "a:set:report"}))
async def cb_stats_chat(call: CallbackQuery, state: FSMContext, deps) -> None:
    back = "a:set" if call.data == "a:set:report" else "a:stat"
    await state.set_state(ReportChatSet.waiting_value)
    await state.update_data(back=back)
    current = (await deps.settings.get("report_chat_id")).strip() or "не задан"
    await show(
        call,
        "📤 <b>Чат для пересылки таблицы</b>\n\n"
        f"Сейчас: <code>{current}</code>\n\n"
        "Пришли <code>@username</code>, числовой ID или просто перешли сюда любое "
        "сообщение из нужного чата.\n\n"
        "⚠️ Личному пользователю бот сможет писать, только если тот уже нажимал "
        "/start у бота — если это не так, заведи для отчётов отдельную группу "
        "и добавь туда бота.\n\n"
        "Чтобы очистить — отправь <code>-</code>",
        kb([[("⬅️ Отмена", back)]]),
    )
    await call.answer()


async def _after_report_chat(message, state: FSMContext, deps) -> None:
    """Вернуть админа туда, откуда он пришёл: в настройки или в статистику."""
    back = (await state.get_data()).get("back", "a:stat")
    await state.clear()
    if back == "a:set":
        from bot.handlers.admin.settings import settings_screen

        await settings_screen(message, deps)
    else:
        await stats_screen(message, deps)


@router.message(ReportChatSet.waiting_value)
async def on_report_chat_value(message: Message, state: FSMContext, deps) -> None:
    raw: str | int | None = None
    if message.forward_from_chat:
        raw = message.forward_from_chat.id
    elif message.forward_from:
        raw = message.forward_from.id
    elif message.text:
        raw = message.text.strip()

    if raw in ("-", "", None):
        await deps.settings.set("report_chat_id", "")
        await _after_report_chat(message, state, deps)
        return

    if isinstance(raw, str) and not raw.lstrip("-").isdigit() and not raw.startswith("@"):
        raw = f"@{raw.split('/')[-1]}"
    chat_id = int(raw) if str(raw).lstrip("-").isdigit() else raw

    try:
        chat = await message.bot.get_chat(chat_id)
    except Exception as exc:  # noqa: BLE001 — показываем причину и даём попробовать снова
        await message.answer(
            f"Не получилось найти этот чат: {exc}\n\n"
            "Если это личный пользователь — он должен сначала сам написать боту "
            "/start. Пришли значение ещё раз.",
            reply_markup=kb([[("⬅️ Отмена", (await state.get_data()).get("back", "a:stat"))]]),
        )
        return

    await deps.settings.set("report_chat_id", str(chat.id))
    await _after_report_chat(message, state, deps)


@router.callback_query(F.data == "a:stat:reset")
async def cb_stats_reset_confirm(call: CallbackQuery, deps) -> None:
    text = (
        "🧹 <b>Обнулить статистику</b>\n\n"
        "Удалит всех пользователей, очередь прогрева и историю рассылок.\n"
        "Медиатека, материал, шаги воронки и настройки останутся как есть.\n\n"
        "Это нужно один раз — перед стартом на боевом канале, чтобы тестовые "
        "заходы не мешали реальной статистике.\n\n"
        "⚠️ Действие необратимо. Точно обнулить?"
    )
    await show(
        call,
        text,
        kb([[("✅ Да, обнулить", "a:stat:reset:go"), ("✖️ Отмена", "a:stat")]]),
    )
    await call.answer()


@router.callback_query(F.data == "a:stat:reset:go")
async def cb_stats_reset_go(call: CallbackQuery, deps) -> None:
    await deps.users.reset_all_stats()
    await call.answer("Статистика обнулена", show_alert=True)
    await stats_screen(call, deps)
