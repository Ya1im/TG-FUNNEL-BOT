TABLES = {
    "users", "media", "funnel_steps", "material_blocks",
    "user_steps", "broadcasts", "broadcast_targets", "settings",
}


async def test_schema_applies(db):
    rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r["name"] for r in rows}
    assert TABLES <= names


async def test_schema_is_idempotent(db):
    await db.apply_schema()
    rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    assert len({r["name"] for r in rows} & TABLES) == len(TABLES)


async def test_wal_enabled(db):
    mode = await db.fetchval("PRAGMA journal_mode")
    assert mode.lower() == "wal"


async def test_old_database_gets_new_columns_and_keeps_gated_behaviour(tmp_path):
    import sqlite3

    from bot.db import Database

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE funnel_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT, position INTEGER NOT NULL,
            delay_seconds INTEGER NOT NULL, requires_subscription INTEGER NOT NULL DEFAULT 0,
            text TEXT, media_id INTEGER, buttons_json TEXT,
            enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL);
        INSERT INTO funnel_steps(position, delay_seconds, requires_subscription, text, created_at)
            VALUES (1, 0, 1, 'закрытый', 0), (2, 0, 0, 'обычный', 0);
        CREATE TABLE broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL, created_by INTEGER,
            segment TEXT NOT NULL DEFAULT 'all', segment_value TEXT, messages_json TEXT NOT NULL,
            scheduled_at INTEGER, status TEXT NOT NULL DEFAULT 'draft',
            started_at INTEGER, finished_at INTEGER);
        INSERT INTO broadcasts(created_at, messages_json) VALUES (0, '[]');
        """
    )
    con.commit()
    con.close()

    db = await Database(path).connect()
    try:
        rows = await db.fetchall("SELECT requires_subscription, on_unsub FROM funnel_steps ORDER BY id")
        # уже настроенный шаг «только подписчикам» продолжает напоминать, как раньше
        assert [(r[0], r[1]) for r in rows] == [(1, "remind"), (0, "skip")]
        assert (await db.fetchone("SELECT sub_mode FROM broadcasts"))["sub_mode"] == "off"
        await db.apply_schema()  # повторный запуск ничего не ломает
    finally:
        await db.close()
