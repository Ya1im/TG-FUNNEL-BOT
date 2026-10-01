"""Шаги прогрева и очередь их отправки конкретным пользователям."""
from __future__ import annotations

import asyncio
import json
import time

from bot.content import ContentBlock
from bot.db import Database

FAST_STEP_SECONDS = 10  # ускоренный тестовый режим: 10 секунд между постами
# Срок у шага, до которого очередь ещё не дошла: срок появляется, когда закрыт предыдущий шаг
NOT_SCHEDULED = 9_000_000_000
_CHUNK = 400


class FunnelRepo:
    def __init__(self, db: Database, admin_ids: tuple[int, ...] = ()) -> None:
        self.db = db
        self.admin_ids = tuple(admin_ids or ())
        self._messages_lock = asyncio.Lock()

    # --- шаги -------------------------------------------------------------

    async def add_step(
        self,
        delay_seconds: int,
        text: str | None = None,
        media_id: int | None = None,
        buttons: list[dict] | None = None,
        requires_subscription: bool = False,
        on_unsub: str = "skip",
        backfill: bool = True,
        extra_messages: list[dict] | None = None,
    ) -> int:
        """backfill=False — массовая загрузка всей цепочки разом (import_json, seed): здесь это не
        «добавили один новый шаг», а пересборка с нуля, задним числом никому ничего не шлём."""
        position = int(
            await self.db.fetchval("SELECT COALESCE(MAX(position), 0) + 1 FROM funnel_steps", default=1)
        )
        step_id = await self.db.execute(
            "INSERT INTO funnel_steps(position, delay_seconds, requires_subscription, on_unsub, "
            "text, media_id, buttons_json, extra_messages_json, enabled, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (
                position,
                int(delay_seconds),
                1 if requires_subscription else 0,
                on_unsub if on_unsub in ("skip", "remind") else "skip",
                text,
                media_id,
                json.dumps(buttons or [], ensure_ascii=False),
                _dump_extras(extra_messages),
                int(time.time()),
            ),
        )
        if backfill:
            await self.backfill_step(step_id)
        return step_id

    async def backfill_step(
        self, step_id: int, position: int | None = None, delay_seconds: int | None = None,
        now: int | None = None,
    ) -> int:
        """Ставит уже существующим активным пользователям только что созданный/включённый шаг —
        минуя тех, кто уже получил более поздний шаг. Срок у него появится, когда до него дойдёт
        очередь: тем, у кого цепочка закончена, — «через задержку шага после добавления»."""
        now = int(now if now is not None else time.time())
        step = await self.get_step(step_id)
        if step is None:
            return 0
        pos = int(step["position"])
        later_sent = (
            "EXISTS (SELECT 1 FROM user_steps us JOIN funnel_steps fs ON fs.id = us.step_id "
            "WHERE us.user_id = {uid} AND fs.position > ? AND us.status = 'sent')"
        )
        # шаг, включённый заново: устаревшую строку тем, кто ушёл дальше, не оживляем
        await self.db.conn.execute(
            "UPDATE user_steps SET status = 'skipped', last_error = 'уже получил более поздний шаг' "
            "WHERE step_id = ? AND status = 'pending' AND " + later_sent.format(uid="user_steps.user_id"),
            (step_id, pos),
        )
        cur = await self.db.conn.execute(
            "INSERT OR IGNORE INTO user_steps(user_id, step_id, due_at, status) "
            "SELECT u.tg_id, ?, ?, 'pending' FROM users u "
            "WHERE u.material_sent_at IS NOT NULL AND u.status = 'active' "
            "AND NOT " + later_sent.format(uid="u.tg_id"),
            (step_id, NOT_SCHEDULED, pos),
        )
        added = cur.rowcount or 0
        await self.db.conn.commit()
        affected = await self._users_with_pending(step_id)
        await self.normalize_users(affected, now)
        return added

    async def _users_with_pending(self, step_id: int) -> list[int]:
        rows = await self.db.fetchall(
            "SELECT DISTINCT user_id FROM user_steps WHERE step_id = ? AND status = 'pending'", (step_id,)
        )
        return [r["user_id"] for r in rows]

    async def normalize_users(
        self, user_ids, now: int | None = None, rebase_overdue: bool = False
    ) -> int:
        """Приводит очередь людей к цепочке: срок есть только у головного шага — первого
        ожидающего включённого; у остальных срок «пока не назначен». Если головной шаг ещё без срока —
        он назначается «сейчас + задержка шага» (в быстром режиме — 10 секунд).

        rebase_overdue=True — головные шаги с уже прошедшим сроком переносятся на «сейчас + задержка»
        (запуск после простоя/предпрода: отсчёт от момента запуска, без залпа).
        Возвращает, сколько сроков перенесено."""
        now = int(now if now is not None else time.time())
        ids = list(dict.fromkeys(int(u) for u in user_ids))
        rebased = 0
        conn = self.db.conn
        for start in range(0, len(ids), _CHUNK):
            chunk = ids[start : start + _CHUNK]
            marks = ",".join("?" for _ in chunk)
            rows = await self.db.fetchall(
                "SELECT us.id, us.user_id, us.due_at, fs.enabled, fs.delay_seconds, u.funnel_fast "
                "FROM user_steps us JOIN funnel_steps fs ON fs.id = us.step_id "
                "JOIN users u ON u.tg_id = us.user_id "
                f"WHERE us.status = 'pending' AND us.user_id IN ({marks}) "
                "ORDER BY us.user_id, fs.position, fs.id",
                chunk,
            )
            updates: list[tuple[int, int]] = []
            seen_head: set[int] = set()
            for row in rows:
                is_head = row["enabled"] and row["user_id"] not in seen_head
                if not is_head:
                    if row["due_at"] != NOT_SCHEDULED:
                        updates.append((NOT_SCHEDULED, row["id"]))
                    continue
                seen_head.add(row["user_id"])
                delay = FAST_STEP_SECONDS if row["funnel_fast"] else int(row["delay_seconds"])
                if row["due_at"] >= NOT_SCHEDULED:
                    updates.append((now + delay, row["id"]))
                elif rebase_overdue and row["due_at"] < now:
                    updates.append((now + delay, row["id"]))
                    rebased += 1
            if updates:
                await conn.executemany("UPDATE user_steps SET due_at = ? WHERE id = ?", updates)
        await conn.commit()
        return rebased

    async def normalize_all(self, now: int | None = None, rebase_overdue: bool = False) -> int:
        rows = await self.db.fetchall("SELECT DISTINCT user_id FROM user_steps WHERE status = 'pending'")
        return await self.normalize_users([r["user_id"] for r in rows], now, rebase_overdue)

    async def heal_stranded(self, now: int | None = None) -> int:
        """Самолечение: у человека есть ожидающие включённые шаги, но ни у одного нет срока
        (сбой между закрытием шага и назначением следующего). Назначаем «сейчас + задержка»."""
        rows = await self.db.fetchall(
            "SELECT DISTINCT us.user_id FROM user_steps us JOIN funnel_steps fs ON fs.id = us.step_id "
            "WHERE us.status = 'pending' AND fs.enabled = 1 AND NOT EXISTS ("
            "SELECT 1 FROM user_steps u2 JOIN funnel_steps f2 ON f2.id = u2.step_id "
            "WHERE u2.user_id = us.user_id AND u2.status = 'pending' AND f2.enabled = 1 "
            "AND u2.due_at < ?)",
            (NOT_SCHEDULED,),
        )
        ids = [r["user_id"] for r in rows]
        if ids:
            await self.normalize_users(ids, now)
        return len(ids)

    async def head_row(self, user_id: int):
        """Головной шаг человека: первый ожидающий включённый по позиции."""
        return await self.db.fetchone(
            "SELECT us.id, us.due_at, fs.delay_seconds FROM user_steps us "
            "JOIN funnel_steps fs ON fs.id = us.step_id "
            "WHERE us.user_id = ? AND us.status = 'pending' AND fs.enabled = 1 "
            "ORDER BY fs.position, fs.id LIMIT 1",
            (user_id,),
        )

    async def set_fast(self, user_id: int, fast: bool, now: int | None = None) -> None:
        """Тестовый режим: ускоренно (10 секунд между постами) или в реальное время.
        Срок ближайшего поста пересчитывается сразу."""
        now = int(now if now is not None else time.time())
        await self.db.execute("UPDATE users SET funnel_fast = ? WHERE tg_id = ?", (1 if fast else 0, user_id))
        head = await self.head_row(user_id)
        if head is None:
            return
        if fast:
            due = now + FAST_STEP_SECONDS
        else:
            base = await self.db.fetchval(
                "SELECT COALESCE(MAX(us.sent_at), (SELECT material_sent_at FROM users WHERE tg_id = ?)) "
                "FROM user_steps us WHERE us.user_id = ? AND us.status = 'sent'",
                (user_id, user_id),
            )
            due = max(now, int(base) + int(head["delay_seconds"])) if base else now + int(head["delay_seconds"])
        await self.db.execute("UPDATE user_steps SET due_at = ? WHERE id = ?", (due, head["id"]))

    async def skip_wait(self, user_id: int, now: int | None = None) -> bool:
        """«Следующий пост сейчас»: срок головного шага — прямо сейчас. False — ожидающих шагов нет."""
        now = int(now if now is not None else time.time())
        head = await self.head_row(user_id)
        if head is None:
            return False
        await self.db.execute("UPDATE user_steps SET due_at = ? WHERE id = ?", (now, head["id"]))
        return True

    async def user_progress(self, user_id: int):
        """Все включённые шаги и то, что с ними у человека (для панели тестового прогона)."""
        return await self.db.fetchall(
            "SELECT fs.id AS step_id, fs.position, fs.delay_seconds, us.status, us.due_at, us.sent_at, "
            "us.last_error FROM funnel_steps fs "
            "LEFT JOIN user_steps us ON us.step_id = fs.id AND us.user_id = ? "
            "WHERE fs.enabled = 1 ORDER BY fs.position, fs.id",
            (user_id,),
        )

    async def has_fast_pending(self) -> bool:
        return bool(await self.db.fetchval(
            "SELECT 1 FROM user_steps us JOIN users u ON u.tg_id = us.user_id "
            "JOIN funnel_steps fs ON fs.id = us.step_id "
            "WHERE us.status = 'pending' AND fs.enabled = 1 AND u.funnel_fast = 1 LIMIT 1", default=0))

    async def rebase_overdue_heads(self, now: int | None = None) -> int:
        """Выпуск в продакшен/старт после простоя: просроченные головные шаги — на «сейчас + задержка»."""
        return await self.normalize_all(now, rebase_overdue=True)

    async def update_step(self, step_id: int, **fields) -> None:
        allowed = {"delay_seconds", "requires_subscription", "on_unsub", "text", "media_id", "buttons_json", "extra_messages_json", "enabled", "position"}
        before = await self.get_step(step_id) if "enabled" in fields else None
        sets, params = [], []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key} = ?")
                params.append(value)
        if not sets:
            return
        params.append(step_id)
        await self.db.execute(f"UPDATE funnel_steps SET {', '.join(sets)} WHERE id = ?", params)
        if before is not None and int(before["enabled"]) == 0 and int(fields["enabled"]) == 1:
            await self.backfill_step(step_id)
        elif before is not None and int(before["enabled"]) == 1 and int(fields["enabled"]) == 0:
            # выключенный шаг не должен держать цепочку
            await self.normalize_users(await self._users_with_pending(step_id))
        elif "position" in fields:
            await self.normalize_all()
        if "delay_seconds" in fields:
            await self._retime_waiting(step_id)

    async def _retime_waiting(self, step_id: int, now: int | None = None) -> None:
        """Задержку шага поменяли — у тех, кто его сейчас ждёт (он головной), срок считается заново:
        «последняя отправка человека + новая задержка», но не раньше «сейчас» (залпа в прошлое нет)."""
        now = int(now if now is not None else time.time())
        step = await self.get_step(step_id)
        if step is None or not int(step["enabled"]):
            return
        for user_id in await self._users_with_pending(step_id):
            head = await self.head_row(user_id)
            user = await self.db.fetchone("SELECT funnel_fast, material_sent_at FROM users WHERE tg_id = ?", (user_id,))
            if head is None or user is None or user["funnel_fast"]:
                continue
            head_step = await self.db.fetchval("SELECT step_id FROM user_steps WHERE id = ?", (head["id"],))
            if int(head_step) != int(step_id):
                continue
            base = await self.db.fetchval(
                "SELECT MAX(sent_at) FROM user_steps WHERE user_id = ? AND status = 'sent'", (user_id,)
            ) or user["material_sent_at"]
            if not base:
                continue
            due = max(now, int(base) + int(step["delay_seconds"]))
            await self.db.execute("UPDATE user_steps SET due_at = ? WHERE id = ?", (due, head["id"]))

    async def set_messages(self, step_id: int, items: list[dict]) -> None:
        """Заменить все сообщения шага: первое — в прежние колонки, остальные — в extra_messages_json."""
        if not items:
            raise ValueError("В шаге должно остаться хотя бы одно сообщение")
        first = items[0]
        media_id = first.get("media_id")
        if media_id is not None and not await self.db.fetchval("SELECT 1 FROM media WHERE id = ?", (media_id,)):
            media_id = None  # файл удалили из медиатеки — ссылка на него уже недействительна
        if media_id is None and first.get("file_id"):
            media_id = await self.db.fetchval("SELECT id FROM media WHERE file_id = ? LIMIT 1", (first["file_id"],))
        await self.db.execute(
            "UPDATE funnel_steps SET text = ?, media_id = ?, buttons_json = ?, extra_messages_json = ? WHERE id = ?",
            (
                first.get("text"),
                media_id,
                json.dumps(first.get("buttons") or [], ensure_ascii=False),
                _dump_extras(items[1:]),
                step_id,
            ),
        )

    async def edit_messages(self, step_id: int, mutate) -> list[dict] | None:
        """Прочитать сообщения шага, изменить и записать — под замком: альбом из нескольких файлов
        приходит параллельными апдейтами, и без замка последняя запись затирала бы остальные.
        mutate(items) правит список на месте; вернёт False — ничего не пишем. None — шага нет."""
        async with self._messages_lock:
            row = await self.get_step_row(step_id)
            if row is None:
                return None
            items = step_messages(row)
            if mutate(items) is False:
                return items
            await self.set_messages(step_id, items)
            return items

    async def get_step(self, step_id: int):
        return await self.db.fetchone("SELECT * FROM funnel_steps WHERE id = ?", (step_id,))

    async def list_steps(self, only_enabled: bool = False):
        where = "WHERE enabled = 1" if only_enabled else ""
        return await self.db.fetchall(
            f"SELECT fs.*, m.slug AS media_slug, m.kind AS media_kind, m.file_id AS media_file_id "
            f"FROM funnel_steps fs LEFT JOIN media m ON m.id = fs.media_id {where} "
            f"ORDER BY fs.position, fs.id"
        )

    async def get_step_row(self, step_id: int):
        """Шаг вместе с данными медиа первого сообщения (для step_messages)."""
        return await self.db.fetchone(
            "SELECT fs.*, m.slug AS media_slug, m.kind AS media_kind, m.file_id AS media_file_id "
            "FROM funnel_steps fs LEFT JOIN media m ON m.id = fs.media_id WHERE fs.id = ?",
            (step_id,),
        )

    async def delete_step(self, step_id: int) -> None:
        affected = await self._users_with_pending(step_id)
        await self.db.execute("DELETE FROM user_steps WHERE step_id = ?", (step_id,))
        await self.db.execute("DELETE FROM funnel_steps WHERE id = ?", (step_id,))
        await self.normalize_users(affected)

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
        await self.normalize_all()

    # --- очередь ----------------------------------------------------------

    async def enqueue(self, user_id: int, fast: bool | None = None, now: int | None = None) -> int:
        """Поставить пользователю все включённые шаги. Повтор не плодит дубли.
        Срок получает только первый шаг — «сейчас + его задержка»; остальные — по цепочке."""
        now = int(now if now is not None else time.time())
        steps = await self.list_steps(only_enabled=True)
        created = 0
        for step in steps:
            cur = await self.db.conn.execute(
                "INSERT OR IGNORE INTO user_steps(user_id, step_id, due_at, status) "
                "VALUES(?, ?, ?, 'pending')",
                (user_id, step["id"], NOT_SCHEDULED),
            )
            created += cur.rowcount or 0
        if fast is not None:
            # флаг режима трогаем только по явной просьбе: обычная выдача урока сохраняет выбранный тестовый режим
            await self.db.conn.execute(
                "UPDATE users SET funnel_fast = ? WHERE tg_id = ?", (1 if fast else 0, user_id)
            )
        await self.db.conn.commit()
        await self.normalize_users([user_id], now)
        return created

    async def still_due(self, queue_id: int, now: int | None = None) -> bool:
        """Строку могли пропустить или перенести уже после выборки пачки в тике."""
        now = int(now if now is not None else time.time())
        return (
            await self.db.fetchval(
                "SELECT COUNT(*) FROM user_steps WHERE id = ? AND status = 'pending' AND due_at <= ?",
                (queue_id, now),
                default=0,
            )
            or 0
        ) > 0

    async def due_steps(
        self, now: int | None = None, limit: int = 200, only_users: "set[int] | None" = None
    ):
        """only_users — предпрод: отправляем только этим людям (None — всем)."""
        now = int(now if now is not None else time.time())
        params: list = [now]
        user_filter = ""
        if only_users is not None:
            if not only_users:
                return []
            user_filter = f"AND us.user_id IN ({','.join('?' for _ in only_users)}) "
            params.extend(sorted(only_users))
        params.append(limit)
        return await self.db.fetchall(
            "SELECT us.id AS queue_id, us.user_id, us.step_id, us.attempts, us.due_at, "
            "fs.text, fs.buttons_json, fs.extra_messages_json, fs.media_id, fs.requires_subscription, fs.on_unsub, fs.position, "
            "m.kind AS media_kind, m.file_id AS media_file_id, "
            "u.first_name, u.username, u.is_subscribed "
            "FROM user_steps us "
            "JOIN funnel_steps fs ON fs.id = us.step_id "
            "JOIN users u ON u.tg_id = us.user_id "
            "LEFT JOIN media m ON m.id = fs.media_id "
            "WHERE us.status = 'pending' AND us.due_at <= ? "
            "AND u.status = 'active' AND fs.enabled = 1 "
            + user_filter
            + "ORDER BY us.due_at LIMIT ?",
            params,
        )

    async def mark_sent(self, queue_id: int, now: int | None = None) -> None:
        now = int(now if now is not None else time.time())
        await self.db.execute(
            "UPDATE user_steps SET status = 'sent', sent_at = ? WHERE id = ?", (now, queue_id)
        )
        await self._advance(queue_id, now)

    async def _advance(self, queue_id: int, now: int) -> None:
        """Шаг закрыт — следующий по цепочке получает срок «сейчас + его задержка»."""
        user_id = await self.db.fetchval("SELECT user_id FROM user_steps WHERE id = ?", (queue_id,))
        if user_id is not None:
            await self.normalize_users([user_id], now)

    async def postpone(self, queue_id: int, seconds: int, error: str | None = None) -> None:
        await self.db.execute(
            "UPDATE user_steps SET due_at = ?, attempts = attempts + 1, last_error = ? WHERE id = ?",
            (int(time.time()) + int(seconds), error, queue_id),
        )

    async def finish(
        self, queue_id: int, status: str, error: str | None = None, now: int | None = None
    ) -> None:
        now = int(now if now is not None else time.time())
        await self.db.execute(
            "UPDATE user_steps SET status = ?, last_error = ?, attempts = attempts + 1 WHERE id = ?",
            (status, error, queue_id),
        )
        await self._advance(queue_id, now)

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
        slugs = {r["id"]: r["slug"] for r in await self.db.fetchall("SELECT id, slug FROM media")}
        data = [
            {
                "position": s["position"],
                "delay_seconds": s["delay_seconds"],
                "requires_subscription": bool(s["requires_subscription"]),
                "on_unsub": s["on_unsub"],
                "text": s["text"],
                "media_slug": s["media_slug"],
                "buttons": json.loads(s["buttons_json"] or "[]"),
                "extra_messages": [
                    {
                        "text": m.get("text"),
                        "media_slug": slugs.get(m.get("media_id")),
                        "buttons": m.get("buttons") or [],
                    }
                    for m in step_messages(s)[1:]
                ],
                "enabled": bool(s["enabled"]),
            }
            for s in steps
        ]
        return json.dumps(data, ensure_ascii=False, indent=2)

    async def import_json(self, raw: str, media_repo) -> int:
        """Заменяет цепочку целиком. Медиа ищется по slug — файлы надо залить заранее."""
        data = json.loads(raw)
        # проверяем файл целиком ДО удаления цепочки — иначе битый файл оставит её стёртой
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise ValueError("ожидается список шагов")
        for item in data:
            extras = item.get("extra_messages") or []
            if not isinstance(extras, list) or not all(isinstance(extra, dict) for extra in extras):
                raise ValueError("extra_messages должен быть списком сообщений")
        progress = await self._received_counts()
        await self.db.execute("DELETE FROM user_steps")
        await self.db.execute("DELETE FROM funnel_steps")
        count = 0
        for item in data:
            media_id = None
            if item.get("media_slug"):
                row = await media_repo.get_by_slug(item["media_slug"])
                media_id = row["id"] if row else None
            extras = []
            for extra in item.get("extra_messages") or []:
                extra_media = await media_repo.get_by_slug(extra["media_slug"]) if extra.get("media_slug") else None
                extras.append({
                    "text": extra.get("text"),
                    "media_id": extra_media["id"] if extra_media else None,
                    "media_kind": extra_media["kind"] if extra_media else None,
                    "file_id": extra_media["file_id"] if extra_media else None,
                    "buttons": extra.get("buttons") or [],
                })
            step_id = await self.add_step(
                extra_messages=extras,
                delay_seconds=int(item.get("delay_seconds", 0)),
                text=item.get("text"),
                media_id=media_id,
                buttons=item.get("buttons") or [],
                requires_subscription=bool(item.get("requires_subscription")),
                # старые выгрузки без поля напоминали автоматически — не меняем это молча
                on_unsub=item.get("on_unsub") or ("remind" if item.get("requires_subscription") else "skip"),
                backfill=False,  # массовая пересборка цепочки — не «добавили новый шаг»
            )
            if not item.get("enabled", True):
                await self.update_step(step_id, enabled=0)
            count += 1
        await self._restore_progress(progress)
        return count

    async def _received_counts(self) -> dict[int, int]:
        """Сколько шагов цепочки у каждого человека уже «закрыто» (получен, пропущен): номер последнего
        закрытого шага по порядку цепочки. Нужно, чтобы замена воронки не стирала прогресс."""
        order = {int(s["id"]): index for index, s in enumerate(await self.list_steps(), start=1)}
        rows = await self.db.fetchall("SELECT user_id, step_id FROM user_steps WHERE status != 'pending'")
        done: dict[int, int] = {}
        for row in rows:
            rank = order.get(int(row["step_id"]), 0)
            done[int(row["user_id"])] = max(done.get(int(row["user_id"]), 0), rank)
        return done

    async def _restore_progress(self, done: dict[int, int], now: int | None = None) -> int:
        """После замены цепочки: каждому активному человеку с выданным уроком — первые `done` шагов нового
        списка считаются полученными, остальные включённые ждут своей очереди. Срок получает только
        ближайший («сейчас + задержка»), поэтому пачкой ничего не уходит."""
        steps = list(await self.list_steps())
        users = await self.db.fetchall(
            "SELECT tg_id FROM users WHERE material_sent_at IS NOT NULL AND status = 'active'"
        )
        values = []
        for user in users:
            known = done.get(int(user["tg_id"]), 0)
            for index, step in enumerate(steps, start=1):
                if index <= known:
                    values.append((user["tg_id"], step["id"], NOT_SCHEDULED, "skipped", "получен по прежней воронке"))
                elif step["enabled"]:
                    values.append((user["tg_id"], step["id"], NOT_SCHEDULED, "pending", None))
        if values:
            await self.db.conn.executemany(
                "INSERT OR IGNORE INTO user_steps(user_id, step_id, due_at, status, last_error) VALUES(?, ?, ?, ?, ?)",
                values,
            )
            await self.db.conn.commit()
        affected = [int(u["tg_id"]) for u in users]
        await self.normalize_users(affected, now)
        return len(affected)


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


_EXTRA_KEYS = ("text", "media_kind", "file_id", "buttons", "media_id")


def _dump_extras(items: list[dict] | None) -> str | None:
    cleaned = [{key: item.get(key) for key in _EXTRA_KEYS} for item in (items or [])]
    for entry in cleaned:
        entry["buttons"] = entry["buttons"] or []
    return json.dumps(cleaned, ensure_ascii=False) if cleaned else None


def _row_get(row, key):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _load_json_list(raw) -> list:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


def step_messages(row) -> list[dict]:
    """Все сообщения шага по порядку: первое из прежних колонок, остальные из extra_messages_json."""
    first = {
        "text": row["text"],
        "media_kind": _row_get(row, "media_kind"),
        "file_id": _row_get(row, "media_file_id"),
        "buttons": [b for b in _load_json_list(_row_get(row, "buttons_json")) if isinstance(b, dict)],
        "media_id": _row_get(row, "media_id"),
    }
    extras = [
        {key: item.get(key) for key in _EXTRA_KEYS} | {"buttons": item.get("buttons") or []}
        for item in _load_json_list(_row_get(row, "extra_messages_json"))
        if isinstance(item, dict)
    ]
    return [first, *extras]


def block_from_item(item: dict) -> ContentBlock:
    return ContentBlock(
        text=item.get("text"),
        media_kind=item.get("media_kind"),
        file_id=item.get("file_id"),
        buttons=item.get("buttons") or [],
    )


def blocks_from_row(row) -> list[ContentBlock]:
    """Сообщения шага, готовые к отправке (одно или несколько)."""
    return [block_from_item(item) for item in step_messages(row)]


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
