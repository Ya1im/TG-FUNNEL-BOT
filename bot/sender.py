"""Отправка с лимитом скорости и обработкой ошибок Telegram."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

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
    outcome = SendOutcome(SENT)
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
        if not outcome.ok:
            return outcome
    return outcome
