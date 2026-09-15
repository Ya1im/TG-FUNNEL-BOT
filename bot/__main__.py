"""Точка входа: собирает бота, планировщик и роутеры."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from bot.backup import daily_backup_hook
from bot.broadcast import BroadcastEngine
from bot.config import Config
from bot.db import Database
from bot.deps import Deps
from bot.handlers import build_router
from bot.scheduler import Scheduler

log = logging.getLogger("bot")


def build_dispatcher(deps: Deps) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp["deps"] = deps
    dp.include_router(build_router())
    return dp


async def set_commands(bot: Bot, config: Config) -> None:
    await bot.set_my_commands(
        [BotCommand(command="start", description="Начать")], scope=BotCommandScopeDefault()
    )
    admin_commands = [
        BotCommand(command="start", description="Пройти сценарий как пользователь"),
        BotCommand(command="admin", description="Админка"),
        BotCommand(command="reset", description="Сбросить своё прохождение"),
    ]
    for admin_id in config.admin_ids:
        try:
            await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:  # noqa: BLE001 — админ мог ещё не открыть бота
            log.warning("Не смог выставить команды админу %s", admin_id)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config.from_env()
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    db = await Database(config.db_path).connect()

    deps = Deps.build(config, db, bot)
    deps.engine = BroadcastEngine(bot, deps.users, deps.broadcasts, deps.limiter)
    scheduler = Scheduler(
        bot=bot,
        users=deps.users,
        funnel=deps.funnel,
        settings=deps.settings,
        gate=deps.gate,
        limiter=deps.limiter,
        tick_seconds=config.tick_seconds,
        broadcast_hook=deps.engine.run_scheduled,
        backup_hook=daily_backup_hook(db),
    )
    deps.scheduler = scheduler

    dp = build_dispatcher(deps)
    await set_commands(bot, config)

    me = await bot.me()
    log.info("Запускаю @%s, админы: %s", me.username, config.admin_ids)

    scheduler.start()
    asyncio.create_task(deps.engine.resume_unfinished())
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await scheduler.stop()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлен")
