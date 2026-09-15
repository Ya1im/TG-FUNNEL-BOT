"""Статистика и выгрузка базы."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery

from bot.handlers.admin.common import kb, show

router = Router(name="admin-stats")


@router.callback_query(F.data == "a:stat")
async def cb_stats(call: CallbackQuery, deps) -> None:
    stats = await deps.users.stats()
    sources = await deps.users.sources()
    pending = await deps.funnel.pending_count()
    lines = [f"• {src}: {cnt}" for src, cnt in sources]
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"👥 Всего: {stats['total']}\n"
        f"✅ Активных: {stats['active']}\n"
        f"🚫 Заблокировали бота: {stats['blocked']}\n"
        f"📢 Подписаны на канал: {stats['subscribed']}\n"
        f"🎁 Получили материал: {stats['got_material']}\n"
        f"🆕 Пришли за сутки: {stats['today']}\n"
        f"⏳ В очереди прогрева: {pending}\n\n"
        "<b>Источники:</b>\n" + ("\n".join(lines) if lines else "нет данных")
    )
    await show(
        call,
        text,
        kb([[("📥 Выгрузить CSV", "a:stat:csv")], [("🔄 Обновить", "a:stat")], [("⬅️ Назад", "a:menu")]]),
    )
    await call.answer()


@router.callback_query(F.data == "a:stat:csv")
async def cb_stats_csv(call: CallbackQuery, deps) -> None:
    data = await deps.users.export_csv()
    await call.message.answer_document(
        BufferedInputFile(data, filename="users.csv"),
        caption="База пользователей. Открывается в Excel и Google Таблицах.",
    )
    await call.answer()
