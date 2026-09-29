"""Отзыв публикаций: удалить у людей только что присланные посты прогрева/рассылок.

Точно — по журналу номеров сообщений (`sent_messages`, пишется с этой версии).
Приблизительно — для шагов, отправленных до появления журнала: бот шлёт человеку беззвучное
служебное сообщение, узнаёт его номер и удаляет диапазон предыдущих сообщений по расчётному числу
(сколько сообщений ушло на эти шаги). Telegram позволяет боту удалять сообщения не старше 48 часов.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import asyncio

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from bot.content import ContentBlock

log = logging.getLogger(__name__)

CHUNK = 100                      # deleteMessages принимает до 100 номеров за раз
MAX_AGE_SECONDS = 48 * 3600      # старше Telegram удалять не даёт
PROBE_TEXT = "\u2800"           # braille-пробел: Telegram принимает его как непустое, но человек почти не видит
PROBE_FALLBACK = "·"


@dataclass
class UserPlan:
    exact_ids: list[int] = field(default_factory=list)
    legacy_messages: int = 0     # расчётное число сообщений, у которых нет журнала
    legacy_steps: int = 0


@dataclass
class RecallPlan:
    since: int
    users: dict[int, UserPlan] = field(default_factory=dict)

    @property
    def exact_total(self) -> int:
        return sum(len(u.exact_ids) for u in self.users.values())

    @property
    def legacy_total(self) -> int:
        return sum(u.legacy_messages for u in self.users.values())

    @property
    def is_empty(self) -> bool:
        return not self.users


def messages_per_step(text, media_kind, file_id) -> int:
    """Сколько сообщений уходит на шаг: кружок с текстом — два, длинная подпись — два и т.д."""
    block = ContentBlock(text=text, media_kind=media_kind, file_id=file_id)
    try:
        return max(1, len(block.factories(None, 0, None)))
    except ValueError:
        return 1


async def build_plan(deps, since: int) -> RecallPlan:
    plan = RecallPlan(since=int(since))
    for row in await deps.sent_log.since(since):
        plan.users.setdefault(row["user_id"], UserPlan()).exact_ids.append(row["message_id"])
    legacy = await deps.db.fetchall(
        "SELECT us.user_id, fs.text, m.kind AS media_kind, m.file_id AS media_file_id "
        "FROM user_steps us JOIN funnel_steps fs ON fs.id = us.step_id "
        "LEFT JOIN media m ON m.id = fs.media_id "
        "WHERE us.status = 'sent' AND us.sent_at >= ? "
        "AND NOT EXISTS (SELECT 1 FROM sent_messages sm WHERE sm.user_id = us.user_id "
        "AND sm.kind = 'step' AND sm.ref_id = us.step_id)",
        (int(since),),
    )
    for row in legacy:
        user_plan = plan.users.setdefault(row["user_id"], UserPlan())
        user_plan.legacy_steps += 1
        user_plan.legacy_messages += messages_per_step(row["text"], row["media_kind"], row["media_file_id"])
    return plan


async def _acquire(deps) -> None:
    if getattr(deps, "limiter", None) is not None:
        await deps.limiter.acquire()


async def _delete(bot, deps, chat_id: int, ids: list[int]) -> None:
    """Удалить пачками по 100. «Сообщение не найдено» — уже удалено человеком, это не ошибка."""
    for start in range(0, len(ids), CHUNK):
        chunk = ids[start : start + CHUNK]
        for attempt in range(3):
            await _acquire(deps)
            try:
                await bot.delete_messages(chat_id, chunk)
                break
            except TelegramRetryAfter as exc:
                if attempt == 2:
                    raise
                await asyncio.sleep(exc.retry_after + 0.5)
            except TelegramBadRequest as exc:
                log.info("Пачка сообщений у %s уже удалена или недоступна: %s", chat_id, exc)
                break


async def _probe(bot, deps, chat_id: int):
    await _acquire(deps)
    try:
        return await bot.send_message(chat_id, PROBE_TEXT, disable_notification=True)
    except TelegramBadRequest:
        await _acquire(deps)
        return await bot.send_message(chat_id, PROBE_FALLBACK, disable_notification=True)


async def run_recall(bot, deps, since: int, cancel_pending: bool = True, now: int | None = None) -> dict[str, int]:
    """Выполнить отзыв. Возвращает счётчики для отчёта админу."""
    now = int(now if now is not None else time.time())
    since = max(int(since), now - MAX_AGE_SECONDS)
    plan = await build_plan(deps, since)
    failed_users: set[int] = set()
    result = {"users": 0, "exact": 0, "ranged": 0, "blocked": 0, "failed": 0, "cancelled": 0}
    for user_id, user_plan in plan.users.items():
        try:
            if user_plan.exact_ids:
                await _delete(bot, deps, user_id, sorted(set(user_plan.exact_ids)))
                result["exact"] += len(user_plan.exact_ids)
            if user_plan.legacy_messages:
                probe = await _probe(bot, deps, user_id)
                last = probe.message_id
                first = max(1, last - user_plan.legacy_messages)
                await _delete(bot, deps, user_id, list(range(first, last + 1)))
                result["ranged"] += user_plan.legacy_messages
            result["users"] += 1
        except TelegramForbiddenError:
            result["blocked"] += 1
        except TelegramAPIError as exc:
            log.warning("Не смог отозвать сообщения у %s: %s", user_id, exc)
            failed_users.add(user_id)
            result["failed"] += 1
    # Отозванные шаги считаем не доставленными. У тех, у кого удалить не вышло, оставляем и шаги,
    # и журнал — чтобы повторный отзыв мог попробовать ещё раз.
    keep = sorted(failed_users)
    marks = ",".join("?" for _ in keep)
    await deps.db.execute(
        "UPDATE user_steps SET status = 'skipped', last_error = 'отозвано' "
        "WHERE status = 'sent' AND sent_at >= ?" + (f" AND user_id NOT IN ({marks})" if keep else ""),
        (since, *keep),
    )
    await deps.sent_log.delete_since(since, keep_users=keep)
    if cancel_pending:
        cur = await deps.db.conn.execute(
            "UPDATE user_steps SET status = 'skipped', last_error = 'отозвано' "
            "WHERE status = 'pending' AND due_at <= ?",
            (now,),
        )
        await deps.db.conn.commit()
        result["cancelled"] = cur.rowcount or 0
    return result
