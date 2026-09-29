"""Панель тестового прогона /test: владелец, админы с полным доступом и тестовые аккаунты предпрода."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.deps import Deps
from bot.handlers.admin.common import kb, show
from bot.services import deliver_material_once, send_welcome
from bot.tester import is_tester, panel_text

log = logging.getLogger(__name__)
router = Router(name="tester")

PANEL_KB_ROWS = [
    [("🔄 Начать заново", "t:new"), ("⏭ Сразу к прогреву", "t:lesson")],
    [("⏩ Следующий пост сейчас", "t:next")],
    [("⚡ Ускоренно", "t:fast"), ("🐢 Реальное время", "t:real")],
    [("🔃 Обновить", "t:ref")],
]


async def _screen(target, deps: Deps, user_id: int) -> None:
    await show(target, await panel_text(deps, user_id), kb(PANEL_KB_ROWS))


async def _guard(call: CallbackQuery, deps: Deps) -> bool:
    if await is_tester(deps, call.from_user.id):
        return True
    await call.answer("Недоступно", show_alert=True)
    return False


@router.message(Command("test"))
async def cmd_test(message: Message, deps: Deps, state: FSMContext) -> None:
    if not await is_tester(deps, message.from_user.id):
        return  # для остальных команды как будто нет
    await state.clear()
    await _screen(message, deps, message.from_user.id)


@router.callback_query(F.data.in_({"t:open", "t:ref"}))
async def cb_open(call: CallbackQuery, deps: Deps, state: FSMContext) -> None:
    if not await _guard(call, deps):
        return
    await state.clear()
    await _screen(call, deps, call.from_user.id)
    await call.answer()


@router.callback_query(F.data == "t:new")
async def cb_new(call: CallbackQuery, deps: Deps) -> None:
    if not await _guard(call, deps):
        return
    user = call.from_user
    await deps.users.upsert(user.id, user.username, user.first_name)
    await deps.users.reset(user.id)
    await call.answer("Сбросил, начинаем с приветствия")
    await call.message.answer("🔄 Прохождение сброшено — вот приветствие. Панель: /test")
    await send_welcome(call.bot, deps, call.message.chat.id)


@router.callback_query(F.data == "t:lesson")
async def cb_lesson(call: CallbackQuery, deps: Deps) -> None:
    if not await _guard(call, deps):
        return
    user = call.from_user
    await deps.users.upsert(user.id, user.username, user.first_name)
    delivered = await deliver_material_once(call.bot, deps, user.id, send_invite=False)
    await call.answer("Урок выдан, воронка запущена" if delivered else "Урок уже выдан", show_alert=not delivered)
    await _screen(call, deps, user.id)


@router.callback_query(F.data == "t:next")
async def cb_next(call: CallbackQuery, deps: Deps) -> None:
    if not await _guard(call, deps):
        return
    user_id = call.from_user.id
    if not await deps.funnel.skip_wait(user_id):
        await call.answer("Ожидающих постов нет", show_alert=True)
        return
    sent = 0
    try:
        sent = await deps.scheduler.run_user(user_id) if deps.scheduler is not None else 0
    except Exception:  # noqa: BLE001
        log.exception("Не смог отправить следующий пост тестировщику %s", user_id)
    await call.answer("Пост отправлен" if sent else "Пост придёт в течение минуты")
    await _screen(call, deps, user_id)


@router.callback_query(F.data.in_({"t:fast", "t:real"}))
async def cb_speed(call: CallbackQuery, deps: Deps) -> None:
    if not await _guard(call, deps):
        return
    fast = call.data == "t:fast"
    await deps.funnel.set_fast(call.from_user.id, fast)
    await call.answer("Ускоренно: 10 секунд между постами" if fast else "Реальное время")
    await _screen(call, deps, call.from_user.id)
