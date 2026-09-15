"""Подсказка админу на сообщение вне сценария. Включается последним."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import Message

router = Router(name="admin-fallback")


@router.message(StateFilter(None), ~F.text.startswith("/"))
async def hint(message: Message) -> None:
    await message.answer(
        "Чтобы залить файл, собрать прогрев или запустить рассылку — открой /admin.\n"
        "Посмотреть сценарий глазами пользователя — /start."
    )
