"""Движок массовой рассылки: копирует сообщения админа всем получателям."""
from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable

from bot.content import ContentBlock
from bot.preprod import allowed_user_ids
from bot.reminder import send_reminder
from bot.sender import BLOCKED, SENT, safe_send, send_block

log = logging.getLogger(__name__)

BATCH = 100
SUB_CACHE_SECONDS = 300  # свежую проверку подписки в пределах рассылки не повторяем
PROGRESS_EVERY = 5.0  # секунд между обновлениями прогресса


class BroadcastEngine:
    def __init__(
        self, bot, users, broadcasts, limiter=None, now: Callable[[], float] = time.time,
        gate=None, settings=None, sent_log=None,
    ):
        self.bot = bot
        self.users = users
        self.broadcasts = broadcasts
        self.limiter = limiter
        self.now = now
        self.gate = gate
        self.settings = settings
        self.sent_log = sent_log

    async def _allowed(self):
        if self.settings is None:
            return None
        return await allowed_user_ids(self.settings, self.users.admin_ids)

    async def prepare(
        self,
        created_by: int,
        segment: str,
        messages: list[dict],
        segment_value: str | None = None,
        scheduled_at: int | None = None,
        sub_mode: str = "off",
    ) -> tuple[int, int]:
        """Создать рассылку и зафиксировать список получателей."""
        broadcast_id = await self.broadcasts.create(
            created_by, segment, messages, segment_value, scheduled_at, sub_mode
        )
        user_ids = await self.users.segment_ids(segment, segment_value)
        allowed = await self._allowed()
        if allowed is not None:  # предпрод: только тестовые аккаунты
            user_ids = [u for u in user_ids if u in allowed]
        await self.broadcasts.set_targets(broadcast_id, user_ids)
        return broadcast_id, len(user_ids)

    async def run(
        self,
        broadcast_id: int,
        progress: Callable[[dict], Awaitable[None]] | None = None,
    ) -> dict[str, int]:
        broadcast = await self.broadcasts.get(broadcast_id)
        if broadcast is None or broadcast["status"] in ("done", "cancelled"):
            return await self.broadcasts.stats(broadcast_id)

        messages = await self.broadcasts.messages(broadcast_id)
        sub_mode = broadcast["sub_mode"] if "sub_mode" in broadcast.keys() else "off"
        await self.broadcasts.set_status(broadcast_id, "running")
        last_progress = 0.0

        while True:
            targets = await self.broadcasts.pending_targets(broadcast_id, BATCH)
            if not targets:
                break
            allowed = await self._allowed()
            for user_id in targets:
                if allowed is not None and user_id not in allowed:
                    # предпрод включили, пока рассылка ждала: настоящим людям ничего не уходит
                    await self.broadcasts.mark_target(broadcast_id, user_id, "skipped", "предпрод")
                    continue
                status, error = await self._deliver(user_id, messages, sub_mode, broadcast_id)
                await self.broadcasts.mark_target(broadcast_id, user_id, status, error)
                if progress and self.now() - last_progress >= PROGRESS_EVERY:
                    last_progress = self.now()
                    await progress(await self.broadcasts.stats(broadcast_id))
            current = await self.broadcasts.get(broadcast_id)
            if current is None or current["status"] == "cancelled":
                return await self.broadcasts.stats(broadcast_id)

        await self.broadcasts.set_status(broadcast_id, "done")
        stats = await self.broadcasts.stats(broadcast_id)
        if progress:
            await progress(stats)
        log.info("Рассылка #%s завершена: %s", broadcast_id, stats)
        return stats

    async def _deliver(
        self, user_id: int, messages: list[dict], sub_mode: str, broadcast_id: int | None = None
    ) -> tuple[str, str | None]:
        """Проверка подписки (если админ включил её для этой рассылки) и отправка."""
        if sub_mode != "off" and self.gate is not None:
            state = await self.gate.status(user_id, cached_seconds=SUB_CACHE_SECONDS)
            if state == "error":
                return "failed", "не смог проверить подписку"
            if state == "no":
                if sub_mode == "remind" and self.settings is not None:
                    outcome = await send_reminder(
                        self.bot, self.settings, self.users, self.limiter, user_id, "resub_reminder_text"
                    )
                    if outcome.status == BLOCKED:
                        return BLOCKED, None
                    return ("reminded", None) if outcome.ok else ("failed", outcome.error)
                return "skipped", None
        return await self._send_to(user_id, messages, broadcast_id)

    async def _send_to(
        self, user_id: int, messages: list[dict], broadcast_id: int | None = None
    ) -> tuple[str, str | None]:
        user = None
        for msg in messages:
            if "chat_id" in msg and "message_id" in msg:
                # Старый формат (до кнопок и редактирования) — копия исходного
                # сообщения админа как есть.
                def action(msg=msg):
                    return self.bot.copy_message(
                        chat_id=user_id,
                        from_chat_id=msg["chat_id"],
                        message_id=msg["message_id"],
                    )

                outcome = await safe_send(
                    action, chat_id=user_id, users=self.users, limiter=self.limiter
                )
                copied = getattr(outcome.result, "message_id", None) if outcome.ok else None
                if isinstance(copied, int):
                    outcome.message_ids = [copied]
            else:
                # Новый формат: текст/медиа/кнопки — как у материала и прогрева,
                # с тем же запасным вариантом для отклонённых кружков.
                if user is None:
                    user = await self.users.get(user_id)
                block = ContentBlock(
                    text=msg.get("text"),
                    media_kind=msg.get("media_kind"),
                    file_id=msg.get("file_id"),
                    buttons=msg.get("buttons") or [],
                )
                outcome = await send_block(
                    block, self.bot, user_id, user=user, users=self.users, limiter=self.limiter
                )
            if self.sent_log is not None and outcome.message_ids:
                await self.sent_log.add(
                    user_id, outcome.message_ids, "broadcast", broadcast_id, int(self.now())
                )
            if outcome.status == BLOCKED:
                return BLOCKED, None
            if not outcome.ok:
                return "failed", outcome.error
        return SENT, None

    async def resume_unfinished(self) -> None:
        """После рестарта контейнера дослать то, что не успели."""
        for row in await self.broadcasts.unfinished():
            log.info("Продолжаю оборванную рассылку #%s", row["id"])
            await self.run(row["id"])

    async def run_scheduled(self) -> None:
        """Хук для планировщика: запустить рассылки, которым пришло время."""
        for row in await self.broadcasts.due_scheduled(int(self.now())):
            log.info("Запускаю запланированную рассылку #%s", row["id"])
            await self.run(row["id"])
