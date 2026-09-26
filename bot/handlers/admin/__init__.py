"""Админка: точка входа и главное меню."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.handlers.admin import broadcast, capture, fallback, funnel, material, media, repeat_start, settings, stats
from bot.handlers.admin.common import AdminFilter, kb, show

router = Router(name="admin")
router.message.filter(AdminFilter())
router.callback_query.filter(AdminFilter())

MENU = kb(
    [
        [("📤 Рассылка", "a:bc")],
        [("🎬 Медиатека", "a:media"), ("🔥 Прогрев", "a:fun")],
        [("🎁 Материал", "a:mat"), ("⚙️ Настройки", "a:set")],
        [("📊 Статистика", "a:stat")],
    ]
)


async def menu_text(deps) -> str:
    stats_data = await deps.users.stats()
    steps = len(await deps.funnel.list_steps(only_enabled=True))
    channel = (await deps.settings.get("channel_id")).strip() or "не задан"
    return (
        "🛠 <b>Админка</b>\n\n"
        "Здесь управляешь ботом целиком: медиатека, воронка прогрева, материал, "
        "рассылки и настройки.\n\n"
        f"👥 Пользователей: {stats_data['total']} "
        f"(активных {stats_data['active']}, заблокировали {stats_data['blocked']})\n"
        f"📢 Канал для проверки: {channel}\n"
        f"🔥 Активных шагов прогрева: {steps}\n\n"
        "Выбери раздел 👇"
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message, deps, state: FSMContext) -> None:
    await state.clear()
    await show(message, await menu_text(deps), MENU)


@router.callback_query(F.data == "a:menu")
async def cb_menu(call: CallbackQuery, deps, state: FSMContext) -> None:
    await state.clear()
    await show(call, await menu_text(deps), MENU)
    await call.answer()


router.include_router(media.router)
router.include_router(funnel.router)
router.include_router(material.router)
router.include_router(repeat_start.router)
router.include_router(settings.router)
router.include_router(stats.router)
router.include_router(broadcast.router)
router.include_router(capture.router)  # перехватывает медиа мимо сценария — до общей подсказки
router.include_router(fallback.router)  # всегда последним
