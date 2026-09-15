"""Хендлеры пользователя: /start, проверка подписки, /reset."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from bot.deps import Deps
from bot.keyboards import CHECK_CALLBACK
from bot.services import check_subscription_flow, send_welcome, start_flow

log = logging.getLogger(__name__)
router = Router(name="user")


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, deps: Deps) -> None:
    payload = (command.args or "").strip()[:64] or None
    await start_flow(message.bot, deps, message.from_user, message.chat.id, payload)


@router.callback_query(F.data == CHECK_CALLBACK)
async def cb_check_subscription(call: CallbackQuery, deps: Deps) -> None:
    ok = await check_subscription_flow(
        call.bot, deps, call.from_user.id, call.message.chat.id
    )
    if ok:
        await call.answer(await deps.settings.get("subscribed_ok_alert"))
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001 — сообщение могло устареть
            pass
    else:
        await call.answer(await deps.settings.get("not_subscribed_alert"), show_alert=True)


@router.message(Command("reset"))
async def cmd_reset(message: Message, deps: Deps) -> None:
    """Сброс своего прохождения — чтобы протестировать воронку заново."""
    if not deps.config.is_admin(message.from_user.id):
        return
    await deps.users.reset(message.from_user.id)
    await message.answer("Твоё прохождение сброшено. Жми /start — пройдёшь заново.")
    await send_welcome(message.bot, deps, message.chat.id)


@router.message(Command("help"))
async def cmd_help(message: Message, deps: Deps) -> None:
    if deps.config.is_admin(message.from_user.id):
        await message.answer(
            "Команды:\n"
            "/start — сценарий как у пользователя\n"
            "/admin — админка (медиатека, прогрев, рассылки, настройки)\n"
            "/reset — сбросить своё прохождение и пройти воронку заново"
        )
    else:
        await message.answer("Жми /start 🙂")


@router.message(F.text | F.photo | F.video | F.document | F.voice | F.video_note)
async def fallback(message: Message, deps: Deps) -> None:
    """Любое сообщение не по сценарию — мягко возвращаем в воронку."""
    user = await deps.users.get(message.from_user.id)
    if user and user["material_sent_at"]:
        await message.answer(await deps.settings.get("already_started_text"))
    else:
        await send_welcome(message.bot, deps, message.chat.id)
