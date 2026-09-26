"""Блоки ответа на повторный /start — та же механика, что у материала.

Раньше на повторный /start уходил один фиксированный текст (настройка
already_started_text). Теперь админ собирает произвольную последовательность
сообщений любого формата (текст, фото/видео/кружок/документ и т.п., с
кнопками-ссылками) — так же, как блоки материала."""
from __future__ import annotations

import json
import time

from bot.db import Database


class RepeatStartRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add_block(
        self,
        text: str | None = None,
        media_id: int | None = None,
        buttons: list[dict] | None = None,
    ) -> int:
        position = int(
            await self.db.fetchval(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM repeat_start_blocks", default=1
            )
        )
        return await self.db.execute(
            "INSERT INTO repeat_start_blocks(position, text, media_id, buttons_json, enabled, created_at) "
            "VALUES(?, ?, ?, ?, 1, ?)",
            (position, text, media_id, json.dumps(buttons or [], ensure_ascii=False), int(time.time())),
        )

    async def list_blocks(self, only_enabled: bool = False):
        where = "WHERE rb.enabled = 1" if only_enabled else ""
        return await self.db.fetchall(
            f"SELECT rb.*, m.slug AS media_slug, m.kind AS media_kind, m.file_id AS media_file_id "
            f"FROM repeat_start_blocks rb LEFT JOIN media m ON m.id = rb.media_id {where} "
            f"ORDER BY rb.position, rb.id"
        )

    async def get_block(self, block_id: int):
        return await self.db.fetchone("SELECT * FROM repeat_start_blocks WHERE id = ?", (block_id,))

    async def delete_block(self, block_id: int) -> None:
        await self.db.execute("DELETE FROM repeat_start_blocks WHERE id = ?", (block_id,))

    async def update_block(self, block_id: int, **fields) -> None:
        allowed = {"text", "media_id", "buttons_json", "enabled", "position"}
        sets, params = [], []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key} = ?")
                params.append(value)
        if not sets:
            return
        params.append(block_id)
        await self.db.execute(f"UPDATE repeat_start_blocks SET {', '.join(sets)} WHERE id = ?", params)

    async def move_block(self, block_id: int, direction: int) -> None:
        blocks = list(await self.list_blocks())
        ids = [b["id"] for b in blocks]
        if block_id not in ids:
            return
        idx = ids.index(block_id)
        new_idx = idx + direction
        if not 0 <= new_idx < len(ids):
            return
        ids[idx], ids[new_idx] = ids[new_idx], ids[idx]
        for position, bid in enumerate(ids, start=1):
            await self.db.execute(
                "UPDATE repeat_start_blocks SET position = ? WHERE id = ?", (position, bid)
            )

    async def count(self) -> int:
        return int(await self.db.fetchval("SELECT COUNT(*) FROM repeat_start_blocks", default=0))
