"""Медиатека: файлы, загруженные админом, хранятся как file_id."""
from __future__ import annotations

import time

from bot.db import Database

KIND_TITLES = {
    "photo": "фото",
    "video": "видео",
    "video_note": "кружок",
    "document": "файл",
    "audio": "аудио",
    "voice": "голосовое",
    "animation": "гифка",
    "sticker": "стикер",
}


class MediaRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def save(
        self,
        slug: str,
        kind: str,
        file_id: str,
        file_unique_id: str | None = None,
        caption: str | None = None,
    ) -> int:
        await self.db.execute(
            "INSERT INTO media(slug, kind, file_id, file_unique_id, caption, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(slug) DO UPDATE SET kind = excluded.kind, file_id = excluded.file_id, "
            "file_unique_id = excluded.file_unique_id, caption = excluded.caption",
            (slug, kind, file_id, file_unique_id, caption, int(time.time())),
        )
        return int(await self.db.fetchval("SELECT id FROM media WHERE slug = ?", (slug,)))

    async def get(self, media_id: int):
        return await self.db.fetchone("SELECT * FROM media WHERE id = ?", (media_id,))

    async def get_by_slug(self, slug: str):
        return await self.db.fetchone("SELECT * FROM media WHERE slug = ?", (slug,))

    async def list(self, limit: int = 50, offset: int = 0):
        return await self.db.fetchall(
            "SELECT * FROM media ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
        )

    async def count(self) -> int:
        return int(await self.db.fetchval("SELECT COUNT(*) FROM media", default=0))

    async def delete(self, media_id: int) -> None:
        await self.db.execute("DELETE FROM media WHERE id = ?", (media_id,))
