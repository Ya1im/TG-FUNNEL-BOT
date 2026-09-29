"""Журнал номеров отправленных сообщений — чтобы публикации можно было отозвать."""
from __future__ import annotations

import time

from bot.db import Database


class SentLogRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(
        self, user_id: int, message_ids: list[int], kind: str, ref_id: int | None = None,
        now: int | None = None,
    ) -> None:
        if not message_ids:
            return
        now = int(now if now is not None else time.time())
        await self.db.conn.executemany(
            "INSERT INTO sent_messages(user_id, message_id, kind, ref_id, sent_at) VALUES(?, ?, ?, ?, ?)",
            [(user_id, mid, kind, ref_id, now) for mid in message_ids],
        )
        await self.db.conn.commit()

    async def has(self, user_id: int, kind: str, ref_id: int) -> bool:
        return bool(await self.db.fetchval(
            "SELECT COUNT(*) FROM sent_messages WHERE user_id = ? AND kind = ? AND ref_id = ?",
            (user_id, kind, ref_id), default=0))

    async def since(self, since: int):
        return await self.db.fetchall(
            "SELECT id, user_id, message_id, kind, ref_id, sent_at FROM sent_messages "
            "WHERE sent_at >= ? ORDER BY user_id, message_id",
            (int(since),),
        )

    async def delete_since(self, since: int, keep_users=()) -> None:
        keep = list(keep_users)
        marks = ",".join("?" for _ in keep)
        await self.db.execute(
            "DELETE FROM sent_messages WHERE sent_at >= ?" + (f" AND user_id NOT IN ({marks})" if keep else ""),
            (int(since), *keep),
        )

    async def purge_older_than(self, before: int) -> None:
        """Telegram всё равно не даёт удалять сообщения старше 48 часов — журнал не копим."""
        await self.db.execute("DELETE FROM sent_messages WHERE sent_at < ?", (int(before),))
