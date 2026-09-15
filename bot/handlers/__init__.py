from aiogram import Router

from bot.handlers import user as user_handlers
from bot.handlers.admin import router as admin_router


def build_router() -> Router:
    router = Router(name="root")
    router.include_router(admin_router)      # админка первой: у неё свои фильтры
    router.include_router(user_handlers.router)
    return router
