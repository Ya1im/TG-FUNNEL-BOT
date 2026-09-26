"""Хендлеры пользователя: /start, проверка подписки, /reset."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, BotCommandScopeChat, CallbackQuery, Message

from bot.deps import Deps
from bot.keyboards import CHECK_CALLBACK
from bot.services import check_subscription_flow, send_welcome, start_flow

log = logging.getLogger(__name__)
router = Router(name="user")


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, deps: Deps) -> None:
    payload = (command.args or "").strip()[:64] or None
    if payload and payload.startswith("v_"):
        await _redeem_viewer_invite(message, deps, payload[2:])
        return
    await _sync_admin_commands(message, deps)
    await start_flow(message.bot, deps, message.from_user, message.chat.id, payload)


async def _redeem_viewer_invite(message: Message, deps: Deps, token: str) -> None:
    """Пригласительная ссылка клиента: даём доступ к /stats. В воронку и статистику человек не попадает."""
    user = message.from_user
    if not await deps.viewers.redeem(token, user.id, user.full_name, user.username):
        await message.answer("Ссылка недействительна или уже использована. Попросите новую.")
        return
    try:
        await message.bot.set_my_commands(
            [BotCommand(command="stats", description="Статистика бота")],
            scope=BotCommandScopeChat(chat_id=message.chat.id),
        )
    except Exception:  # noqa: BLE001 — меню команд не критично
        log.debug("Не смог выставить команды клиенту %s", user.id)
    await message.answer("Готово ✅ Доступ открыт. Отправьте /stats — пришлю статистику бота.")


@router.callback_query(F.data == CHECK_CALLBACK)
async def cb_check_subscription(call: CallbackQuery, deps: Deps) -> None:
    ok = await check_subscription_flow(
        call.bot, deps, call.from_user.id, call.message.chat.id
    )
    if ok:
        # Клавиатуру не убираем: человек должен видеть, что кнопки на месте
        await call.answer(await deps.settings.get("subscribed_ok_alert"))
    else:
        await call.answer(await deps.settings.get("not_subscribed_alert"), show_alert=True)


async def _sync_admin_commands(message: Message, deps: Deps) -> None:
    """Меню команд для админа выставляем при первом заходе — до /start чат не существует."""
    if not deps.config.is_admin(message.from_user.id):
        return
    try:
        await message.bot.set_my_commands(
            [
                BotCommand(command="start", description="Пройти сценарий как пользователь"),
                BotCommand(command="admin", description="Админка"),
                BotCommand(command="reset", description="Сбросить своё прохождение"),
            ],
            scope=BotCommandScopeChat(chat_id=message.chat.id),
        )
    except Exception:  # noqa: BLE001 — не критично
        log.debug("Не смог выставить команды админу %s", message.from_user.id)


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
    elif await deps.viewers.is_viewer(message.from_user.id):
        await message.answer("Команда: /stats — статистика бота.")
    else:
        await message.answer("Жми /start 🙂")


@router.message()
async def fallback(message: Message) -> None:
    """Любое сообщение не по сценарию (текст, медиа, стикеры и т.д.) — просто убираем.

    Ожидаемое действие пользователя — кнопки в меню; всё остальное только
    захламляет переписку. В личных чатах бот может удалять входящие сообщения
    без специальных прав, поэтому чистим тихо, без ответа.
    """
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
