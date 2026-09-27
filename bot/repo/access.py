"""Клиенты с ограниченным доступом: роль «stats» (только /stats) или «admin» (вся админка кроме опасного)."""
from __future__ import annotations

import secrets
import time

from bot.db import Database

INVITE_TTL_SECONDS = 7 * 86400


class AccessRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(
        self, tg_id: int, name: str | None = None, username: str | None = None, role: str = "stats"
    ) -> None:
        await self.db.execute(
            "INSERT INTO viewers(tg_id, name, username, added_at, role) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET name = excluded.name, username = excluded.username, "
            "role = excluded.role",
            (tg_id, name, username, int(time.time()), role),
        )

    async def remove(self, tg_id: int) -> None:
        await self.db.execute("DELETE FROM viewers WHERE tg_id = ?", (tg_id,))

    async def role(self, tg_id: int) -> str | None:
        row = await self.db.fetchone("SELECT role FROM viewers WHERE tg_id = ?", (tg_id,))
        return row["role"] if row is not None else None

    async def list(self):
        return await self.db.fetchall("SELECT * FROM viewers ORDER BY added_at, tg_id")

    async def create_invite(
        self, created_by: int | None = None, role: str = "stats", now: int | None = None
    ) -> str:
        token = secrets.token_urlsafe(9)
        await self.db.execute(
            "INSERT INTO viewer_invites(token, created_by, created_at, role) VALUES(?, ?, ?, ?)",
            (token, created_by, int(now if now is not None else time.time()), role),
        )
        return token

    async def redeem(
        self, token: str, tg_id: int, name: str | None, username: str | None, now: int | None = None
    ) -> bool:
        """Одноразово превращает пригласительную ссылку в доступ. False — ссылка неверна, старая или использована."""
        now = int(now if now is not None else time.time())
        row = await self.db.fetchone(
            "SELECT created_at, used_by, role FROM viewer_invites WHERE token = ?", (token,)
        )
        if row is None or row["used_by"] is not None or now - row["created_at"] > INVITE_TTL_SECONDS:
            return False
        await self.db.execute(
            "UPDATE viewer_invites SET used_by = ?, used_at = ? WHERE token = ?", (tg_id, now, token)
        )
        await self.add(tg_id, name, username, role=row["role"])
        return True
