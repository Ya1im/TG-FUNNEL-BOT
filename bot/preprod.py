"""Предпрод-режим: пока он включён, публикации уходят только тестовым аккаунтам.

Касается только того, что бот шлёт сам «в тираж» — посты прогрева, рассылки и автовыдачу урока.
Приветствие, проверка подписки и выдача материала по кнопке работают как обычно.
"""
from __future__ import annotations

import re

from bot.repo.funnel import human_delay


async def tester_ids(settings, admin_ids=(), db=None) -> set[int]:
    """Тестировщики: владельцы, админы с полным доступом (роль admin) и аккаунты из списка предпрода."""
    listed = {int(x) for x in re.findall(r"\d+", await settings.get("preprod_user_ids"))}
    ids = listed | {int(a) for a in admin_ids}
    if db is not None:
        rows = await db.fetchall("SELECT tg_id FROM viewers WHERE role = 'admin'")
        ids |= {int(r["tg_id"]) for r in rows}
    return ids


async def allowed_user_ids(settings, admin_ids=(), db=None) -> set[int] | None:
    """None — предпрод выключен (получатели любые). Иначе — тестировщики (`tester_ids`)."""
    if (await settings.get("preprod_mode")).strip() != "1":
        return None
    return await tester_ids(settings, admin_ids, db)


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


async def init_chain(funnel, settings, now: int | None = None) -> int:
    """Один раз при первом запуске версии с цепочкой «от предыдущего поста»: очередь всех людей
    перестраивается, просроченные головные шаги отсчитываются от момента запуска — без залпа.
    Возвращает, сколько сроков перенесено (0 — уже выполнено или переносить нечего)."""
    if (await settings.get("chain_initialized")).strip():
        return 0
    rebased = await funnel.rebase_overdue_heads(now)
    await settings.set("chain_initialized", "1")
    return rebased


def human_span(seconds: int) -> str:
    """Точный срок словами: «1 д 20 ч 30 мин»."""
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = [f"{days} д" if days else "", f"{hours} ч" if hours else "", f"{minutes} мин" if minutes else ""]
    return " ".join(p for p in parts if p) or f"{seconds} сек"


async def timing_note(funnel, step_id: int) -> str | None:
    """Пометка для тестового аккаунта: через сколько этот пост пришёл бы настоящему человеку."""
    steps = await funnel.list_steps(only_enabled=True)
    total, cumulative = len(steps), 0
    for index, step in enumerate(steps, start=1):
        cumulative += int(step["delay_seconds"])
        if step["id"] != step_id:
            continue
        delay = human_span(step["delay_seconds"])
        if index == 1:
            when = f"через {delay} после получения урока"
        else:
            when = f"через {delay} после предыдущего поста (≈ через {human_span(cumulative)} после урока)"
        return f"🧪 Предпрод · пост {index} из {total}\nРеальному человеку придёт {when}"
    return None


def parse_ids(raw: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", raw or "")]
