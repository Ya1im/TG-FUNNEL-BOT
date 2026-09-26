"""Клиенты с ограниченным доступом: видят только статистику (/stats), админкой не пользуются."""
from __future__ import annotations

import secrets
import time

from bot.db import Database

INVITE_TTL_SECONDS = 7 * 86400


class ViewersRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(self, tg_id: int, name: str | None = None, username: str | None = None) -> None:
        await self.db.execute(
            "INSERT INTO viewers(tg_id, name, username, added_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET name = excluded.name, username = excluded.username",
            (tg_id, name, username, int(time.time())),
        )

    async def remove(self, tg_id: int) -> None:
        await self.db.execute("DELETE FROM viewers WHERE tg_id = ?", (tg_id,))

    async def is_viewer(self, tg_id: int) -> bool:
        return await self.db.fetchone("SELECT 1 FROM viewers WHERE tg_id = ?", (tg_id,)) is not None

    async def list(self):
        return await self.db.fetchall("SELECT * FROM viewers ORDER BY added_at, tg_id")

    async def create_invite(self, created_by: int | None = None, now: int | None = None) -> str:
        token = secrets.token_urlsafe(9)
        await self.db.execute(
            "INSERT INTO viewer_invites(token, created_by, created_at) VALUES(?, ?, ?)",
            (token, created_by, int(now if now is not None else time.time())),
        )
        return token

    async def redeem(
        self, token: str, tg_id: int, name: str | None, username: str | None, now: int | None = None
    ) -> bool:
        """Одноразово превращает пригласительную ссылку в доступ. False — ссылка неверна, старая или использована."""
        now = int(now if now is not None else time.time())
        row = await self.db.fetchone(
            "SELECT created_at, used_by FROM viewer_invites WHERE token = ?", (token,)
        )
        if row is None or row["used_by"] is not None or now - row["created_at"] > INVITE_TTL_SECONDS:
            return False
        await self.db.execute(
            "UPDATE viewer_invites SET used_by = ?, used_at = ? WHERE token = ?", (tg_id, now, token)
        )
        await self.add(tg_id, name, username)
        return True
