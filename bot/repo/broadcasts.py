"""Рассылки: черновики, снимок получателей, прогресс."""
from __future__ import annotations

import json
import time

from bot.db import Database


class BroadcastsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self,
        created_by: int,
        segment: str,
        messages: list[dict],
        segment_value: str | None = None,
        scheduled_at: int | None = None,
    ) -> int:
        return await self.db.execute(
            "INSERT INTO broadcasts(created_at, created_by, segment, segment_value, "
            "messages_json, scheduled_at, status) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                int(time.time()),
                created_by,
                segment,
                segment_value,
                json.dumps(messages, ensure_ascii=False),
                scheduled_at,
                "queued" if scheduled_at else "draft",
            ),
        )

    async def set_targets(self, broadcast_id: int, user_ids: list[int]) -> int:
        await self.db.executemany(
            "INSERT OR IGNORE INTO broadcast_targets(broadcast_id, user_id) VALUES(?, ?)",
            [(broadcast_id, uid) for uid in user_ids],
        )
        return len(user_ids)

    async def get(self, broadcast_id: int):
        return await self.db.fetchone("SELECT * FROM broadcasts WHERE id = ?", (broadcast_id,))

    async def messages(self, broadcast_id: int) -> list[dict]:
        raw = await self.db.fetchval(
            "SELECT messages_json FROM broadcasts WHERE id = ?", (broadcast_id,)
        )
        return json.loads(raw) if raw else []

    async def set_status(self, broadcast_id: int, status: str) -> None:
        field = {"running": "started_at", "done": "finished_at", "cancelled": "finished_at"}.get(status)
        if field:
            await self.db.execute(
                f"UPDATE broadcasts SET status = ?, {field} = ? WHERE id = ?",
                (status, int(time.time()), broadcast_id),
            )
        else:
            await self.db.execute(
                "UPDATE broadcasts SET status = ? WHERE id = ?", (status, broadcast_id)
            )

    async def pending_targets(self, broadcast_id: int, limit: int = 100) -> list[int]:
        rows = await self.db.fetchall(
            "SELECT user_id FROM broadcast_targets WHERE broadcast_id = ? AND status = 'pending' "
            "LIMIT ?",
            (broadcast_id, limit),
        )
        return [r["user_id"] for r in rows]

    async def mark_target(
        self, broadcast_id: int, user_id: int, status: str, error: str | None = None
    ) -> None:
        await self.db.execute(
            "UPDATE broadcast_targets SET status = ?, error = ? WHERE broadcast_id = ? AND user_id = ?",
            (status, error, broadcast_id, user_id),
        )

    async def stats(self, broadcast_id: int) -> dict[str, int]:
        rows = await self.db.fetchall(
            "SELECT status, COUNT(*) AS cnt FROM broadcast_targets WHERE broadcast_id = ? "
            "GROUP BY status",
            (broadcast_id,),
        )
        stats = {"pending": 0, "sent": 0, "blocked": 0, "failed": 0}
        for row in rows:
            stats[row["status"]] = row["cnt"]
        stats["total"] = sum(stats.values())
        return stats

    async def due_scheduled(self, now: int | None = None):
        now = int(now if now is not None else time.time())
        return await self.db.fetchall(
            "SELECT * FROM broadcasts WHERE status = 'queued' AND scheduled_at IS NOT NULL "
            "AND scheduled_at <= ? ORDER BY scheduled_at",
            (now,),
        )

    async def unfinished(self):
        """Рассылки, оборванные рестартом контейнера."""
        return await self.db.fetchall("SELECT * FROM broadcasts WHERE status = 'running'")

    async def recent(self, limit: int = 10):
        return await self.db.fetchall(
            "SELECT * FROM broadcasts ORDER BY created_at DESC LIMIT ?", (limit,)
        )

    async def cancel(self, broadcast_id: int) -> None:
        await self.set_status(broadcast_id, "cancelled")
