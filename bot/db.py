"""Подключение к SQLite, применение схемы и низкоуровневые хелперы."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable, Sequence

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class Database:
    """Одно соединение на процесс. SQLite в WAL спокойно это выдерживает."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("База не подключена — сначала await db.connect()")
        return self._conn

    async def connect(self) -> "Database":
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self.apply_schema()
        log.info("База подключена: %s", self.path)
        return self

    async def apply_schema(self) -> None:
        await self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        await self._migrate()
        await self.conn.commit()

    async def _has_column(self, table: str, column: str) -> bool:
        cur = await self.conn.execute(f"PRAGMA table_info({table})")
        rows = await cur.fetchall()
        await cur.close()
        return any(r["name"] == column for r in rows)

    async def _migrate(self) -> None:
        """Добавляет колонки в базы, созданные до появления этих полей."""
        if not await self._has_column("funnel_steps", "on_unsub"):
            await self.conn.execute(
                "ALTER TABLE funnel_steps ADD COLUMN on_unsub TEXT NOT NULL DEFAULT 'skip'"
            )
            # Шаги, у которых админ уже включил «только подписчикам», напоминали
            # автоматически — сохраняем это поведение, дальше админ управляет сам.
            await self.conn.execute(
                "UPDATE funnel_steps SET on_unsub = 'remind' WHERE requires_subscription = 1"
            )
        if not await self._has_column("broadcasts", "sub_mode"):
            await self.conn.execute(
                "ALTER TABLE broadcasts ADD COLUMN sub_mode TEXT NOT NULL DEFAULT 'off'"
            )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Выполнить запрос, вернуть lastrowid."""
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur.lastrowid or 0

    async def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        await self.conn.executemany(sql, list(seq))
        await self.conn.commit()

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return list(rows)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        cur = await self.conn.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row

    async def fetchval(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = await self.fetchone(sql, params)
        return row[0] if row is not None else default
