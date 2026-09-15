"""Пользователи: регистрация, статусы, сегменты, статистика."""
from __future__ import annotations

import csv
import io
import time

from bot.db import Database

SEGMENTS = {
    "all": "status = 'active'",
    "subscribed": "status = 'active' AND is_subscribed = 1",
    "not_subscribed": "status = 'active' AND is_subscribed = 0",
    "got_material": "status = 'active' AND material_sent_at IS NOT NULL",
    "no_material": "status = 'active' AND material_sent_at IS NULL",
    "blocked": "status = 'blocked'",
}


class UsersRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def upsert(
        self,
        tg_id: int,
        username: str | None = None,
        first_name: str | None = None,
        source: str | None = None,
    ) -> bool:
        """Создать или обновить пользователя. Возвращает True, если он новый.

        Источник (utm из deep-link) пишется только при первом заходе —
        повторный /start без payload не должен его затирать.
        """
        now = int(time.time())
        existing = await self.db.fetchone("SELECT tg_id FROM users WHERE tg_id = ?", (tg_id,))
        if existing is None:
            await self.db.execute(
                "INSERT INTO users(tg_id, username, first_name, started_at, source, status) "
                "VALUES(?, ?, ?, ?, ?, 'active')",
                (tg_id, username, first_name, now, source),
            )
            return True
        await self.db.execute(
            "UPDATE users SET username = ?, first_name = ?, "
            "status = CASE WHEN status = 'blocked' THEN 'active' ELSE status END, "
            "source = COALESCE(source, ?) WHERE tg_id = ?",
            (username, first_name, source, tg_id),
        )
        return False

    async def get(self, tg_id: int):
        return await self.db.fetchone("SELECT * FROM users WHERE tg_id = ?", (tg_id,))

    async def set_status(self, tg_id: int, status: str) -> None:
        await self.db.execute("UPDATE users SET status = ? WHERE tg_id = ?", (status, tg_id))

    async def mark_blocked(self, tg_id: int) -> None:
        """Пользователь заблокировал бота: снимаем его с прогрева."""
        await self.db.execute("UPDATE users SET status = 'blocked' WHERE tg_id = ?", (tg_id,))
        await self.db.execute(
            "UPDATE user_steps SET status = 'skipped', last_error = 'user blocked bot' "
            "WHERE user_id = ? AND status = 'pending'",
            (tg_id,),
        )

    async def set_subscription(self, tg_id: int, is_subscribed: bool) -> None:
        await self.db.execute(
            "UPDATE users SET is_subscribed = ?, sub_checked_at = ? WHERE tg_id = ?",
            (1 if is_subscribed else 0, int(time.time()), tg_id),
        )

    async def mark_material_sent(self, tg_id: int) -> None:
        now = int(time.time())
        await self.db.execute(
            "UPDATE users SET material_sent_at = COALESCE(material_sent_at, ?), "
            "funnel_started_at = COALESCE(funnel_started_at, ?) WHERE tg_id = ?",
            (now, now, tg_id),
        )

    async def reset(self, tg_id: int) -> None:
        """Сброс прохождения — для повторного теста воронки."""
        await self.db.execute(
            "UPDATE users SET material_sent_at = NULL, funnel_started_at = NULL, "
            "is_subscribed = 0, sub_checked_at = NULL, status = 'active' WHERE tg_id = ?",
            (tg_id,),
        )
        await self.db.execute("DELETE FROM user_steps WHERE user_id = ?", (tg_id,))

    async def segment_ids(self, segment: str, value: str | None = None) -> list[int]:
        if segment == "source":
            rows = await self.db.fetchall(
                "SELECT tg_id FROM users WHERE status = 'active' AND source = ?", (value,)
            )
        else:
            where = SEGMENTS.get(segment, SEGMENTS["all"])
            rows = await self.db.fetchall(f"SELECT tg_id FROM users WHERE {where}")
        return [r["tg_id"] for r in rows]

    async def segment_count(self, segment: str, value: str | None = None) -> int:
        return len(await self.segment_ids(segment, value))

    async def stats(self) -> dict[str, int]:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS total,"
            " SUM(status = 'active') AS active,"
            " SUM(status = 'blocked') AS blocked,"
            " SUM(status = 'active' AND is_subscribed = 1) AS subscribed,"
            " SUM(material_sent_at IS NOT NULL) AS got_material,"
            " SUM(started_at > strftime('%s','now') - 86400) AS today"
            " FROM users"
        )
        return {k: int(row[k] or 0) for k in row.keys()}

    async def sources(self) -> list[tuple[str, int]]:
        rows = await self.db.fetchall(
            "SELECT COALESCE(source, '—') AS src, COUNT(*) AS cnt FROM users "
            "GROUP BY src ORDER BY cnt DESC LIMIT 30"
        )
        return [(r["src"], r["cnt"]) for r in rows]

    async def export_csv(self) -> bytes:
        rows = await self.db.fetchall(
            "SELECT tg_id, username, first_name, started_at, source, status, "
            "is_subscribed, material_sent_at FROM users ORDER BY started_at"
        )
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=";")
        writer.writerow(
            ["tg_id", "username", "имя", "старт", "источник", "статус", "подписан", "материал выдан"]
        )
        for r in rows:
            writer.writerow(
                [
                    r["tg_id"],
                    r["username"] or "",
                    r["first_name"] or "",
                    _fmt(r["started_at"]),
                    r["source"] or "",
                    r["status"],
                    "да" if r["is_subscribed"] else "нет",
                    _fmt(r["material_sent_at"]),
                ]
            )
        return buf.getvalue().encode("utf-8-sig")


def _fmt(ts: int | None) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
