from aiogram import Router

from bot.handlers import tester as tester_handlers
from bot.handlers import user as user_handlers
from bot.handlers import viewer as viewer_handlers
from bot.handlers.admin import router as admin_router


def build_router() -> Router:
    router = Router(name="root")
    # /test — до админки: иначе у админа посреди диалога (FSM) команда уйдёт в диалог как текст
    router.include_router(tester_handlers.router)
    router.include_router(admin_router)      # админка: у неё свои фильтры
    router.include_router(viewer_handlers.router)  # /stats — до user: там последний обработчик глотает всё
    router.include_router(user_handlers.router)
    return router
