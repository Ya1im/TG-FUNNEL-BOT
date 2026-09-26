"""Движок массовой рассылки: копирует сообщения админа всем получателям."""
from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable

from bot.content import ContentBlock
from bot.sender import BLOCKED, SENT, safe_send, send_block

log = logging.getLogger(__name__)

BATCH = 100
PROGRESS_EVERY = 5.0  # секунд между обновлениями прогресса


class BroadcastEngine:
    def __init__(self, bot, users, broadcasts, limiter=None, now: Callable[[], float] = time.time):
        self.bot = bot
        self.users = users
        self.broadcasts = broadcasts
        self.limiter = limiter
        self.now = now

    async def prepare(
        self,
        created_by: int,
        segment: str,
        messages: list[dict],
        segment_value: str | None = None,
        scheduled_at: int | None = None,
    ) -> tuple[int, int]:
        """Создать рассылку и зафиксировать список получателей."""
        broadcast_id = await self.broadcasts.create(
            created_by, segment, messages, segment_value, scheduled_at
        )
        user_ids = await self.users.segment_ids(segment, segment_value)
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
        await self.broadcasts.set_status(broadcast_id, "running")
        last_progress = 0.0

        while True:
            targets = await self.broadcasts.pending_targets(broadcast_id, BATCH)
            if not targets:
                break
            for user_id in targets:
                status, error = await self._send_to(user_id, messages)
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

    async def _send_to(self, user_id: int, messages: list[dict]) -> tuple[str, str | None]:
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
