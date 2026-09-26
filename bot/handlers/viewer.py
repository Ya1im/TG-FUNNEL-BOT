"""/stats — текстовая статистика для клиентов (доступ «только статистика») и админов."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject

from bot.stats_report import build_report

router = Router(name="viewer")

REFRESH = "v:stats"


class CanViewStats(BaseFilter):
    """Пропускает админов и клиентов из списка доступа; остальным команда «не существует»."""

    async def __call__(self, event: TelegramObject, deps=None) -> bool:
        user = getattr(event, "from_user", None)
        if not (user and deps):
            return False
        return deps.config.is_admin(user.id) or await deps.viewers.is_viewer(user.id)


router.message.filter(CanViewStats())
router.callback_query.filter(CanViewStats())

MARKUP = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Обновить", callback_data=REFRESH)]])


@router.message(Command("stats"))
async def cmd_stats(message: Message, deps) -> None:
    await message.answer(await build_report(deps), reply_markup=MARKUP)


@router.callback_query(F.data == REFRESH)
async def cb_refresh(call: CallbackQuery, deps) -> None:
    try:
        await call.message.edit_text(await build_report(deps), reply_markup=MARKUP)
    except TelegramBadRequest:
        pass  # цифры не изменились — Telegram не даёт «отредактировать» тем же текстом
    await call.answer("Обновил")
