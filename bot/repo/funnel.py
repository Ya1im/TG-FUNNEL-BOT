"""Шаги прогрева и очередь их отправки конкретным пользователям."""
from __future__ import annotations

import json
import time

from bot.content import ContentBlock
from bot.db import Database

FAST_STEP_SECONDS = 10  # «прогнать на себе»: задержки по 10 секунд


class FunnelRepo:
    def __init__(self, db: Database, admin_ids: tuple[int, ...] = ()) -> None:
        self.db = db
        self.admin_ids = tuple(admin_ids or ())

    # --- шаги -------------------------------------------------------------

    async def add_step(
        self,
        delay_seconds: int,
        text: str | None = None,
        media_id: int | None = None,
        buttons: list[dict] | None = None,
        requires_subscription: bool = False,
        on_unsub: str = "skip",
    ) -> int:
        position = int(
            await self.db.fetchval("SELECT COALESCE(MAX(position), 0) + 1 FROM funnel_steps", default=1)
        )
        return await self.db.execute(
            "INSERT INTO funnel_steps(position, delay_seconds, requires_subscription, on_unsub, "
            "text, media_id, buttons_json, enabled, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (
                position,
                int(delay_seconds),
                1 if requires_subscription else 0,
                on_unsub if on_unsub in ("skip", "remind") else "skip",
                text,
                media_id,
                json.dumps(buttons or [], ensure_ascii=False),
                int(time.time()),
            ),
        )

    async def update_step(self, step_id: int, **fields) -> None:
        allowed = {"delay_seconds", "requires_subscription", "on_unsub", "text", "media_id", "buttons_json", "enabled", "position"}
        sets, params = [], []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key} = ?")
                params.append(value)
        if not sets:
            return
        params.append(step_id)
        await self.db.execute(f"UPDATE funnel_steps SET {', '.join(sets)} WHERE id = ?", params)

    async def get_step(self, step_id: int):
        return await self.db.fetchone("SELECT * FROM funnel_steps WHERE id = ?", (step_id,))

    async def list_steps(self, only_enabled: bool = False):
        where = "WHERE enabled = 1" if only_enabled else ""
        return await self.db.fetchall(
            f"SELECT fs.*, m.slug AS media_slug, m.kind AS media_kind, m.file_id AS media_file_id "
            f"FROM funnel_steps fs LEFT JOIN media m ON m.id = fs.media_id {where} "
            f"ORDER BY fs.position, fs.id"
        )

    async def delete_step(self, step_id: int) -> None:
        await self.db.execute("DELETE FROM user_steps WHERE step_id = ?", (step_id,))
        await self.db.execute("DELETE FROM funnel_steps WHERE id = ?", (step_id,))

    async def move_step(self, step_id: int, direction: int) -> None:
        """Поменять шаг местами с соседним (direction: -1 вверх, +1 вниз)."""
        steps = list(await self.list_steps())
        ids = [s["id"] for s in steps]
        if step_id not in ids:
            return
        idx = ids.index(step_id)
        new_idx = idx + direction
        if not 0 <= new_idx < len(ids):
            return
        ids[idx], ids[new_idx] = ids[new_idx], ids[idx]
        for position, sid in enumerate(ids, start=1):
            await self.db.execute("UPDATE funnel_steps SET position = ? WHERE id = ?", (position, sid))

    # --- очередь ----------------------------------------------------------

    async def enqueue(self, user_id: int, fast: bool = False, now: int | None = None) -> int:
        """Поставить пользователю все включённые шаги. Повтор не плодит дубли."""
        now = int(now if now is not None else time.time())
        steps = await self.list_steps(only_enabled=True)
        created = 0
        for order, step in enumerate(steps, start=1):
            delay = FAST_STEP_SECONDS * order if fast else int(step["delay_seconds"])
            cur = await self.db.conn.execute(
                "INSERT OR IGNORE INTO user_steps(user_id, step_id, due_at, status) "
                "VALUES(?, ?, ?, 'pending')",
                (user_id, step["id"], now + delay),
            )
            created += cur.rowcount or 0
        await self.db.conn.commit()
        return created

    async def due_steps(self, now: int | None = None, limit: int = 200):
        now = int(now if now is not None else time.time())
        return await self.db.fetchall(
            "SELECT us.id AS queue_id, us.user_id, us.step_id, us.attempts, us.due_at, "
            "fs.text, fs.buttons_json, fs.requires_subscription, fs.on_unsub, fs.position, "
            "m.kind AS media_kind, m.file_id AS media_file_id, "
            "u.first_name, u.username, u.is_subscribed "
            "FROM user_steps us "
            "JOIN funnel_steps fs ON fs.id = us.step_id "
            "JOIN users u ON u.tg_id = us.user_id "
            "LEFT JOIN media m ON m.id = fs.media_id "
            "WHERE us.status = 'pending' AND us.due_at <= ? "
            "AND u.status = 'active' AND fs.enabled = 1 "
            "ORDER BY us.due_at LIMIT ?",
            (now, limit),
        )

    async def mark_sent(self, queue_id: int) -> None:
        await self.db.execute(
            "UPDATE user_steps SET status = 'sent', sent_at = ? WHERE id = ?",
            (int(time.time()), queue_id),
        )

    async def postpone(self, queue_id: int, seconds: int, error: str | None = None) -> None:
        await self.db.execute(
            "UPDATE user_steps SET due_at = ?, attempts = attempts + 1, last_error = ? WHERE id = ?",
            (int(time.time()) + int(seconds), error, queue_id),
        )

    async def finish(self, queue_id: int, status: str, error: str | None = None) -> None:
        await self.db.execute(
            "UPDATE user_steps SET status = ?, last_error = ?, attempts = attempts + 1 WHERE id = ?",
            (status, error, queue_id),
        )

    async def pending_count(self, user_id: int | None = None) -> int:
        if user_id is None:
            if self.admin_ids:
                placeholders = ",".join("?" for _ in self.admin_ids)
                return int(await self.db.fetchval(
                    "SELECT COUNT(*) FROM user_steps "
                    f"WHERE status = 'pending' AND user_id NOT IN ({placeholders})",
                    tuple(self.admin_ids), default=0))
            return int(await self.db.fetchval(
                "SELECT COUNT(*) FROM user_steps WHERE status = 'pending'", default=0))
        return int(await self.db.fetchval(
            "SELECT COUNT(*) FROM user_steps WHERE status = 'pending' AND user_id = ?",
            (user_id,), default=0))

    async def clear_user(self, user_id: int) -> None:
        await self.db.execute("DELETE FROM user_steps WHERE user_id = ?", (user_id,))

    # --- экспорт/импорт ---------------------------------------------------

    async def export_json(self) -> str:
        steps = await self.list_steps()
        data = [
            {
                "position": s["position"],
                "delay_seconds": s["delay_seconds"],
                "requires_subscription": bool(s["requires_subscription"]),
                "on_unsub": s["on_unsub"],
                "text": s["text"],
                "media_slug": s["media_slug"],
                "buttons": json.loads(s["buttons_json"] or "[]"),
                "enabled": bool(s["enabled"]),
            }
            for s in steps
        ]
        return json.dumps(data, ensure_ascii=False, indent=2)

    async def import_json(self, raw: str, media_repo) -> int:
        """Заменяет цепочку целиком. Медиа ищется по slug — файлы надо залить заранее."""
        data = json.loads(raw)
        await self.db.execute("DELETE FROM user_steps")
        await self.db.execute("DELETE FROM funnel_steps")
        count = 0
        for item in data:
            media_id = None
            if item.get("media_slug"):
                row = await media_repo.get_by_slug(item["media_slug"])
                media_id = row["id"] if row else None
            step_id = await self.add_step(
                delay_seconds=int(item.get("delay_seconds", 0)),
                text=item.get("text"),
                media_id=media_id,
                buttons=item.get("buttons") or [],
                requires_subscription=bool(item.get("requires_subscription")),
                # старые выгрузки без поля напоминали автоматически — не меняем это молча
                on_unsub=item.get("on_unsub") or ("remind" if item.get("requires_subscription") else "skip"),
            )
            if not item.get("enabled", True):
                await self.update_step(step_id, enabled=0)
            count += 1
        return count


def block_from_row(row) -> ContentBlock:
    """Собрать ContentBlock из строки очереди или списка шагов."""
    buttons = []
    raw = row["buttons_json"]
    if raw:
        try:
            buttons = json.loads(raw)
        except (ValueError, TypeError):
            buttons = []
    return ContentBlock(
        text=row["text"],
        media_kind=row["media_kind"],
        file_id=row["media_file_id"],
        buttons=buttons,
    )


def human_delay(seconds: int) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} сек"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    if seconds < 86400:
        hours = seconds / 3600
        return f"{hours:.0f} ч" if hours == int(hours) else f"{hours:.1f} ч"
    days = seconds / 86400
    return f"{days:.0f} дн" if days == int(days) else f"{days:.1f} дн"


def parse_delay(raw: str) -> int | None:
    """«30м», «2ч», «3д», «90» (минуты), «1д 4ч» → секунды."""
    raw = (raw or "").strip().lower().replace(",", ".")
    if not raw:
        return None
    units = {"с": 1, "c": 1, "s": 1, "м": 60, "m": 60, "ч": 3600, "h": 3600, "д": 86400, "d": 86400}
    total = 0.0
    number = ""
    found = False
    for char in raw:
        if char.isdigit() or char == ".":
            number += char
        elif char in units:
            if number:
                total += float(number) * units[char]
                number = ""
                found = True
        elif char == " ":
            continue
        else:
            return None
    if number:
        total += float(number) * (1 if found else 60)  # голое число — минуты
        found = True
    return int(total) if found else None
