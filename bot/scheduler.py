"""Планировщик: раз в минуту досылает шаги прогрева, которым пришло время."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from bot.keyboards import subscribe_kb
from bot.repo.funnel import block_from_row
from bot.sender import BLOCKED, safe_send, send_block

log = logging.getLogger(__name__)

GATE_RETRY_SECONDS = 6 * 3600     # не подписан — напомним через 6 часов
GATE_MAX_ATTEMPTS = 3             # столько напоминаний, потом шаг пропускаем
ERROR_RETRY_SECONDS = 30 * 60
ERROR_MAX_ATTEMPTS = 3
BATCH = 200


class Scheduler:
    def __init__(
        self,
        *,
        bot,
        users,
        funnel,
        settings,
        gate,
        limiter=None,
        tick_seconds: int = 60,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        broadcast_hook: Callable[[], Awaitable[None]] | None = None,
        backup_hook: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.bot = bot
        self.users = users
        self.funnel = funnel
        self.settings = settings
        self.gate = gate
        self.limiter = limiter
        self.tick_seconds = tick_seconds
        self.now = now
        self.sleep = sleep
        self.broadcast_hook = broadcast_hook
        self.backup_hook = backup_hook
        self._task: asyncio.Task | None = None
        self._stopped = False

    def start(self) -> None:
        self._task = asyncio.create_task(self.run_forever(), name="scheduler")

    async def stop(self) -> None:
        self._stopped = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def run_forever(self) -> None:
        log.info("Планировщик запущен, тик раз в %s сек", self.tick_seconds)
        while not self._stopped:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — тик не имеет права уронить бота
                log.exception("Ошибка в тике планировщика")
            await self.sleep(self.tick_seconds)

    async def tick(self) -> dict[str, int]:
        stats = {"sent": 0, "held": 0, "skipped": 0, "failed": 0, "blocked": 0}
        rows = await self.funnel.due_steps(int(self.now()), limit=BATCH)
        reminded: set[int] = set()
        for row in rows:
            await self._process(row, stats, reminded)
        if self.broadcast_hook is not None:
            await self.broadcast_hook()
        if self.backup_hook is not None:
            await self.backup_hook()
        if any(stats.values()):
            log.info("Тик: %s", stats)
        return stats

    async def _process(self, row, stats: dict[str, int], reminded: set[int]) -> None:
        user_id = row["user_id"]
        queue_id = row["queue_id"]

        if row["requires_subscription"]:
            if not await self.gate.check(user_id):
                if row["attempts"] >= GATE_MAX_ATTEMPTS:
                    await self.funnel.finish(queue_id, "skipped", "нет подписки")
                    stats["skipped"] += 1
                    return
                if user_id not in reminded:
                    await self._send_reminder(user_id)
                    reminded.add(user_id)
                await self.funnel.postpone(queue_id, GATE_RETRY_SECONDS, "нет подписки")
                stats["held"] += 1
                return

        outcome = await send_block(
            block_from_row(row),
            self.bot,
            user_id,
            user=row,
            users=self.users,
            limiter=self.limiter,
        )
        if outcome.ok:
            await self.funnel.mark_sent(queue_id)
            stats["sent"] += 1
        elif outcome.status == BLOCKED:
            stats["blocked"] += 1  # очередь пользователя уже очищена в mark_blocked
        else:
            if row["attempts"] + 1 >= ERROR_MAX_ATTEMPTS:
                await self.funnel.finish(queue_id, "failed", outcome.error)
                stats["failed"] += 1
            else:
                await self.funnel.postpone(queue_id, ERROR_RETRY_SECONDS, outcome.error)
                stats["failed"] += 1

    async def _send_reminder(self, user_id: int) -> None:
        text = await self.settings.get("reminder_text")
        kb = await subscribe_kb(self.settings)

        async def action():
            return await self.bot.send_message(user_id, text, reply_markup=kb)

        await safe_send(action, chat_id=user_id, users=self.users, limiter=self.limiter)
