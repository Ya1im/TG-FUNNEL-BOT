"""Отправка с лимитом скорости и обработкой ошибок Telegram."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from bot import content
from bot.content import is_voice_forbidden

log = logging.getLogger(__name__)

SENT = "sent"
BLOCKED = "blocked"
FAILED = "failed"


@dataclass
class SendOutcome:
    status: str
    error: str | None = None
    result: Any = None
    message_ids: list[int] = field(default_factory=list)  # номера отправленных сообщений (для отзыва)
    caption_split: bool = False       # подпись не влезла в лимит Telegram — текст ушёл вторым сообщением
    custom_emoji_lost: bool = False   # кастомные эмодзи в тексте, но Telegram отдал сообщение без них
    caption_len: int = 0              # видимая длина текста (для пояснения админу)
    copied: bool = False              # длинная подпись ушла одним постом — копией исходного сообщения админа
    copy_failed: bool = False         # копию пробовали, но Telegram отказал — текст ушёл вторым сообщением

    @property
    def ok(self) -> bool:
        return self.status == SENT


class RateLimiter:
    """Не больше N отправок в секунду на весь процесс."""

    def __init__(
        self,
        rate_per_second: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._next_at = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self.interval <= 0:
            return
        async with self._lock:
            now = self._clock()
            wait = self._next_at - now
            if wait > 0:
                await self._sleep(wait)
                now = self._clock()
            self._next_at = max(now, self._next_at) + self.interval


async def safe_send(
    action: Callable[[], Awaitable[Any]],
    *,
    chat_id: int | None = None,
    users=None,
    limiter: RateLimiter | None = None,
    retries: int = 3,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> SendOutcome:
    """Выполнить отправку, пережив 429, сеть и блокировку пользователем.

    403 (пользователь заблокировал бота) не считается ошибкой рассылки:
    помечаем пользователя и снимаем его с прогрева.
    """
    for attempt in range(1, retries + 1):
        if limiter is not None:
            await limiter.acquire()
        try:
            result = await action()
            return SendOutcome(SENT, result=result)
        except TelegramRetryAfter as exc:
            log.warning("429 от Telegram, ждём %s сек", exc.retry_after)
            await sleep(exc.retry_after + 0.5)
        except TelegramForbiddenError as exc:
            if users is not None and chat_id is not None:
                await users.mark_blocked(chat_id)
            return SendOutcome(BLOCKED, error=str(exc))
        except (TelegramNetworkError, TelegramServerError) as exc:
            log.warning("Сеть/сервер Telegram: %s (попытка %s)", exc, attempt)
            await sleep(min(2 ** attempt, 10))
        except TelegramBadRequest as exc:
            log.error("Telegram отказал: %s", exc)
            return SendOutcome(FAILED, error=str(exc))
        except Exception as exc:  # noqa: BLE001 — отправка не должна ронять процесс
            log.exception("Непредвиденная ошибка отправки")
            return SendOutcome(FAILED, error=str(exc))
    return SendOutcome(FAILED, error="исчерпаны попытки отправки")


async def send_block(
    block,
    bot,
    chat_id: int,
    *,
    user=None,
    users=None,
    limiter: RateLimiter | None = None,
) -> SendOutcome:
    """Отправить ContentBlock целиком. Первая неудача прекращает блок.

    Кружок, отклонённый Telegram из-за приватности получателя
    (VOICE_MESSAGES_FORBIDDEN — общая настройка на голосовые и видеосообщения),
    пробуем донести запасным способом — обычным видео (см. ContentBlock.fallback_factory)."""
    copy_failed = False
    if block.caption_overflow(user) and _copy_allowed(block, user):
        # подпись заведомо не влезает: сразу шлём копию исходного сообщения — получится один пост
        outcome = await _send_copy(block, bot, chat_id, user=user, users=users, limiter=limiter)
        if outcome.ok or outcome.status == BLOCKED:
            return outcome
        copy_failed = _copy_refused(outcome, block, user)
    outcome = await _send_block_once(block, bot, chat_id, user=user, users=users, limiter=limiter)
    if not outcome.ok and not outcome.message_ids and block.long_caption(user) and _TOO_LONG_RE.search(outcome.error or ""):
        # Telegram не принял подпись сверх 1024 — запоминаем; сначала пробуем копию, потом привычное деление
        log.info("Подпись длиннее 1024 не принята — дальше длинные подписи шлю копией или делю на два сообщения")
        content.set_caption_limit(content.CAPTION_LIMIT)
        if _copy_allowed(block, user):
            copied = await _send_copy(block, bot, chat_id, user=user, users=users, limiter=limiter)
            if copied.ok or copied.status == BLOCKED:
                return copied
            copy_failed = _copy_refused(copied, block, user)
        outcome = await _send_block_once(block, bot, chat_id, user=user, users=users, limiter=limiter)
    outcome.caption_split = block.caption_overflow(user)
    outcome.copy_failed = copy_failed and outcome.caption_split
    if outcome.caption_split:
        outcome.caption_len = content.visible_len(block.render(user))
    if outcome.ok:
        outcome.custom_emoji_lost = _custom_emoji_lost(block, user, outcome.result)
        if _custom_emoji_checked(block, outcome.result):
            # любая реальная отправка с кастомным эмодзи показывает, принимает ли их Telegram для этого бота
            content.set_custom_emoji_status(not outcome.custom_emoji_lost)
    return outcome


_TOO_LONG_RE = re.compile(r"too[ _]long", re.IGNORECASE)


def _copy_allowed(block, user) -> bool:
    """Копию пробуем, пока Telegram не отказал в ней из-за длины подписи (тогда отказ запомнен до перезапуска)."""
    return block.can_copy(user) and content.get_copy_status() is not False


def _copy_refused(outcome, block, user) -> bool:
    """Копия не прошла. Если из-за длины подписи (в пределах 2048) — запоминаем до перезапуска, чтобы не тратить
    лишний запрос на каждого получателя. Подпись длиннее 2048 не принимает никто — это не повод отключать копии."""
    length = content.visible_len(block.render(user))
    if _TOO_LONG_RE.search(outcome.error or "") and length <= content.PREMIUM_CAPTION_LIMIT:
        content.set_copy_status(False)
    return True


async def _send_copy(block, bot, chat_id, *, user=None, users=None, limiter=None) -> SendOutcome:
    outcome = await safe_send(block.copy_factory(bot, chat_id, user), chat_id=chat_id, users=users, limiter=limiter)
    outcome.message_ids = _message_ids(outcome)
    if outcome.ok:
        outcome.copied = True
        outcome.caption_len = content.visible_len(block.render(user))
        content.set_copy_status(True)
    return outcome


def _message_ids(outcome) -> list[int]:
    """Номера отправленных сообщений из ответа Telegram (одно сообщение или список — альбом, пачка копий)."""
    if not outcome.ok:
        return []
    if isinstance(outcome.result, list):
        return [m.message_id for m in outcome.result if isinstance(getattr(m, "message_id", None), int)]
    message_id = getattr(outcome.result, "message_id", None)
    return [message_id] if isinstance(message_id, int) else []


def _custom_emoji_checked(block, result) -> bool:
    """По ответу Telegram можно судить о кастомных эмодзи: они были в тексте и форма ответа понятна."""
    return "<tg-emoji" in (block.text or "") and (hasattr(result, "entities") or hasattr(result, "caption_entities"))


def _custom_emoji_lost(block, user, result) -> bool:
    """В тексте были кастомные эмодзи, а отправленное сообщение вернулось без custom_emoji-сущностей —
    значит Telegram их не принял (нужен Premium у владельца бота). Если форму ответа не понять — не считаем потерей."""
    if "<tg-emoji" not in (block.text or ""):
        return False
    if not (hasattr(result, "entities") or hasattr(result, "caption_entities")):
        return False
    entities = list(getattr(result, "entities", None) or []) + list(getattr(result, "caption_entities", None) or [])
    return not any(getattr(entity, "type", None) == "custom_emoji" for entity in entities)


async def _send_block_once(block, bot, chat_id, *, user=None, users=None, limiter=None) -> SendOutcome:
    outcome = SendOutcome(SENT)
    ids: list[int] = []
    for index, factory in enumerate(block.factories(bot, chat_id, user)):
        outcome = await safe_send(factory, chat_id=chat_id, users=users, limiter=limiter)
        if not outcome.ok and index == 0 and is_voice_forbidden(outcome.error):
            fallback = block.fallback_factory(bot, chat_id, user)
            if fallback is not None:
                log.info(
                    "Кружок отклонён приватностью получателя (chat_id=%s) — шлю обычным видео",
                    chat_id,
                )
                outcome = await safe_send(fallback, chat_id=chat_id, users=users, limiter=limiter)
        # альбом: Telegram отвечает списком сообщений — номера нужны все, иначе рассылку не отозвать
        ids.extend(_message_ids(outcome))
        if not outcome.ok:
            outcome.message_ids = ids
            return outcome
    outcome.message_ids = ids
    return outcome
