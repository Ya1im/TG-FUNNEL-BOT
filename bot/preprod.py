"""Предпрод-режим: пока он включён, публикации уходят только тестовым аккаунтам.

Касается только того, что бот шлёт сам «в тираж» — посты прогрева, рассылки и автовыдачу урока.
Приветствие, проверка подписки и выдача материала по кнопке работают как обычно.
"""
from __future__ import annotations

import re


async def allowed_user_ids(settings, admin_ids=()) -> set[int] | None:
    """None — предпрод выключен (получатели любые). Иначе — множество допустимых получателей:
    тестовые аккаунты из настроек и админы (им нужно «прогнать на себе»)."""
    if (await settings.get("preprod_mode")).strip() != "1":
        return None
    listed = {int(x) for x in re.findall(r"\d+", await settings.get("preprod_user_ids"))}
    return listed | {int(a) for a in admin_ids}


async def is_enabled(settings) -> bool:
    return (await settings.get("preprod_mode")).strip() == "1"


async def init_preprod(settings) -> bool:
    """Один раз при первом запуске версии с предпродом: включаем его по умолчанию, чтобы
    очередь прогрева сама никому не ушла, пока владелец не выпустит бота в продакшен.
    True — включили сейчас."""
    if (await settings.get("preprod_initialized")).strip():
        return False
    await settings.set("preprod_mode", "1")
    await settings.set("preprod_initialized", "1")
    return True


def parse_ids(raw: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", raw or "")]
