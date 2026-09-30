"""Меню команд (кнопка «Menu» в чате с ботом).

Личный список чата перекрывает общий, поэтому у каждого, кому мы выставляем личный список,
обязательно должна быть /start — иначе она пропадает из меню (так было у клиентов со статистикой).
Обычным людям личный список не нужен: им достаётся общий из `__main__.set_commands`."""
from __future__ import annotations

import asyncio
import logging

from aiogram.types import BotCommand, BotCommandScopeChat

from bot.preprod import tester_ids

log = logging.getLogger(__name__)

DESCRIPTIONS = {
    "start": "Пройти сценарий как пользователь",
    "admin": "Админка",
    "test": "Тестовый прогон воронки",
    "stats": "Статистика бота",
    "reset": "Сбросить своё прохождение",
}


async def menu_commands(deps, user_id: int) -> list[str] | None:
    """Имена команд личного меню. None — личное меню не нужно (человек видит общее)."""
    is_owner = deps.config.is_admin(user_id)
    role = None if is_owner else await deps.access.role(user_id)
    names = ["start"]
    if is_owner or role == "admin":
        names.append("admin")
    if is_owner or role == "admin" or user_id in await tester_ids(deps.settings, deps.config.admin_ids, deps.db):
        names.append("test")
    if role in ("admin", "stats"):
        names.append("stats")
    if is_owner:
        names.append("reset")
    return names if len(names) > 1 or role is not None else None


async def sync_menu(bot, deps, user_id: int, chat_id: int, *, clear_if_none: bool = False) -> None:
    """Выставляет личное меню. Сбой Telegram не критичен — на работу бота меню не влияет."""
    try:
        names = await menu_commands(deps, user_id)
        scope = BotCommandScopeChat(chat_id=chat_id)
        if names is not None:
            await bot.set_my_commands([BotCommand(command=n, description=DESCRIPTIONS[n]) for n in names], scope=scope)
        elif clear_if_none:
            await bot.delete_my_commands(scope=scope)
    except Exception:  # noqa: BLE001
        log.debug("Не смог обновить меню команд для %s", user_id)


async def sync_all_menus(bot, deps, *, pause: float = 0.05) -> None:
    """После запуска обновляет меню всем, у кого оно личное: владельцам, клиентам, тестировщикам.
    Личный чат с человеком имеет тот же id, что и сам человек."""
    viewers = await deps.db.fetchall("SELECT tg_id FROM viewers")
    ids = {int(r["tg_id"]) for r in viewers}
    ids |= await tester_ids(deps.settings, deps.config.admin_ids, deps.db)
    for user_id in sorted(ids):
        await sync_menu(bot, deps, user_id, user_id)
        await asyncio.sleep(pause)
