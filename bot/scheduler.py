"""Планировщик: раз в минуту досылает шаги прогрева, которым пришло время."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from bot.preprod import allowed_user_ids, timing_note, tester_ids
from bot.reminder import send_reminder
from bot.repo.funnel import block_from_row
from bot.sender import BLOCKED, safe_send, send_block

log = logging.getLogger(__name__)

GATE_RETRY_SECONDS = 6 * 3600     # не подписан — напомним через 6 часов
GATE_CACHE_SECONDS = 300         # свежий результат проверки можно не повторять 5 минут
GATE_MAX_ATTEMPTS = 3             # столько напоминаний, потом шаг пропускаем
ERROR_RETRY_SECONDS = 30 * 60
ERROR_MAX_ATTEMPTS = 3
FAST_TICK_SECONDS = 10
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
        sent_log=None,
        tick_seconds: int = 60,
        heal_every_ticks: int = 60,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        deliver_hook: Callable[[], Awaitable[None]] | None = None,
        broadcast_hook: Callable[[], Awaitable[None]] | None = None,
        backup_hook: Callable[[], Awaitable[None]] | None = None,
        stats_export_hook: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.bot = bot
        self.users = users
        self.funnel = funnel
        self.settings = settings
        self.gate = gate
        self.limiter = limiter
        self.sent_log = sent_log
        self.tick_seconds = tick_seconds
        self.heal_every_ticks = heal_every_ticks
        self._ticks = 0
        self.now = now
        self.sleep = sleep
        self.deliver_hook = deliver_hook
        self.broadcast_hook = broadcast_hook
        self.backup_hook = backup_hook
        self.stats_export_hook = stats_export_hook
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
            await self.sleep(await self._sleep_seconds())

    async def _sleep_seconds(self) -> float:
        """Пока у кого-то идёт ускоренный тест (10 с между постами), проверяем очередь чаще."""
        try:
            if await self.funnel.has_fast_pending():
                return min(self.tick_seconds, FAST_TICK_SECONDS)
        except Exception:  # noqa: BLE001
            log.exception("Не смог проверить ускоренные тесты")
        return self.tick_seconds

    async def run_user(self, user_id: int) -> int:
        """Отправить человеку его ближайший созревший пост прямо сейчас (кнопка «Следующий пост сейчас»)."""
        rows = await self.funnel.due_steps(int(self.now()), limit=1, only_users={user_id})
        if not rows:
            return 0
        preprod = (await self.settings.get("preprod_mode")).strip() == "1"
        stats = {"sent": 0, "held": 0, "skipped": 0, "failed": 0, "blocked": 0}
        await self._process(rows[0], stats, set(), preprod=preprod)
        return stats["sent"]

    async def tick(self) -> dict[str, int]:
        stats = {"sent": 0, "held": 0, "skipped": 0, "failed": 0, "blocked": 0}
        if self.deliver_hook is not None:
            try:
                await self.deliver_hook()
            except Exception:  # noqa: BLE001 — автовыдача не должна останавливать прогрев
                log.exception("Ошибка автовыдачи урока")
        if self._ticks % self.heal_every_ticks == 0:
            try:
                healed = await self.funnel.heal_stranded(int(self.now()))
                if healed:
                    log.warning("Самолечение очереди: восстановлена цепочка у %s чел.", healed)
            except Exception:  # noqa: BLE001 — лечение не должно останавливать отправку
                log.exception("Ошибка самолечения очереди")
        self._ticks += 1
        allowed = await allowed_user_ids(self.settings, self.funnel.admin_ids, self.users.db)
        rows = await self.funnel.due_steps(int(self.now()), limit=BATCH, only_users=allowed)
        reminded: set[int] = set()
        for row in rows:
            await self._process(row, stats, reminded, preprod=allowed is not None)
        if self.broadcast_hook is not None:
            await self.broadcast_hook()
        if self.backup_hook is not None:
            await self.backup_hook()
        if self.stats_export_hook is not None:
            await self.stats_export_hook()
        if any(stats.values()):
            log.info("Тик: %s", stats)
        return stats

    async def _process(self, row, stats: dict[str, int], reminded: set[int], preprod: bool = False) -> None:
        user_id = row["user_id"]
        queue_id = row["queue_id"]

        if not await self.funnel.still_due(queue_id, int(self.now())):
            return  # пока ждали очереди, шаг пропустили или перенесли

        if row["requires_subscription"]:
            state = await self.gate.status(user_id, cached_seconds=GATE_CACHE_SECONDS)
            if state == "error":
                # Telegram не ответил — это не «отписался», напоминать нельзя
                if row["attempts"] + 1 >= ERROR_MAX_ATTEMPTS:
                    await self.funnel.finish(queue_id, "failed", "не смог проверить подписку", now=int(self.now()))
                    stats["failed"] += 1
                else:
                    await self.funnel.postpone(queue_id, ERROR_RETRY_SECONDS, "не смог проверить подписку")
                    stats["held"] += 1
                return
            if state == "no":
                if row["on_unsub"] != "remind":
                    await self.funnel.finish(queue_id, "skipped", "нет подписки", now=int(self.now()))
                    stats["skipped"] += 1
                    return
                max_attempts = await self._int_setting("gate_max_attempts", GATE_MAX_ATTEMPTS)
                if row["attempts"] >= max_attempts:
                    await self.funnel.finish(queue_id, "skipped", "нет подписки", now=int(self.now()))
                    stats["skipped"] += 1
                    return
                if user_id not in reminded:
                    await self._send_reminder(user_id)
                    reminded.add(user_id)
                hours = await self._int_setting("gate_retry_hours", GATE_RETRY_SECONDS // 3600)
                await self.funnel.postpone(queue_id, hours * 3600, "нет подписки")
                stats["held"] += 1
                return

        if preprod and not await self._note_already_sent(row):
            await self._send_timing_note(row)
        outcome = await send_block(
            block_from_row(row),
            self.bot,
            user_id,
            user=row,
            users=self.users,
            limiter=self.limiter,
        )
        if self.sent_log is not None and outcome.message_ids:
            await self.sent_log.add(user_id, outcome.message_ids, "step", row["step_id"], int(self.now()))
        if outcome.ok:
            await self.funnel.mark_sent(queue_id, now=int(self.now()))
            stats["sent"] += 1
        elif outcome.status == BLOCKED:
            stats["blocked"] += 1  # очередь пользователя уже очищена в mark_blocked
        else:
            if row["attempts"] + 1 >= ERROR_MAX_ATTEMPTS:
                await self.funnel.finish(queue_id, "failed", outcome.error, now=int(self.now()))
                stats["failed"] += 1
            else:
                await self.funnel.postpone(queue_id, ERROR_RETRY_SECONDS, outcome.error)
                stats["failed"] += 1

    async def _note_already_sent(self, row) -> bool:
        if self.sent_log is None:
            return row["attempts"] > 0
        return await self.sent_log.has(row["user_id"], "step", row["step_id"])

    async def _send_timing_note(self, row) -> None:
        """Предпрод: перед постом — беззвучная пометка «когда это пришло бы человеку»."""
        note = await timing_note(self.funnel, row["step_id"])
        if not note:
            return
        user_id = row["user_id"]
        outcome = await safe_send(
            lambda: self.bot.send_message(user_id, note, disable_notification=True),
            chat_id=user_id, users=self.users, limiter=self.limiter,
        )
        message_id = getattr(outcome.result, "message_id", None) if outcome.ok else None
        if self.sent_log is not None and isinstance(message_id, int):
            await self.sent_log.add(user_id, [message_id], "step", row["step_id"], int(self.now()))

    async def _int_setting(self, key: str, default: int) -> int:
        value = await self.settings.get_int(key)
        return value if value and value > 0 else default

    async def _send_reminder(self, user_id: int) -> None:
        await send_reminder(self.bot, self.settings, self.users, self.limiter, user_id)
