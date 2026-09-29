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


async def test_old_viewers_table_gets_role_column(tmp_path):
    import sqlite3

    from bot.db import Database

    path = tmp_path / "old_viewers.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE viewers (
            tg_id    INTEGER PRIMARY KEY,
            name     TEXT,
            username TEXT,
            added_at INTEGER NOT NULL
        );
        INSERT INTO viewers(tg_id, name, username, added_at) VALUES (5, 'Анна', 'anna', 0);
        CREATE TABLE viewer_invites (
            token      TEXT PRIMARY KEY,
            created_by INTEGER,
            created_at INTEGER NOT NULL,
            used_by    INTEGER,
            used_at    INTEGER
        );
        INSERT INTO viewer_invites(token, created_by, created_at) VALUES ('tok', 99, 0);
        """
    )
    con.commit()
    con.close()

    db = await Database(path).connect()
    try:
        row = await db.fetchone("SELECT role FROM viewers WHERE tg_id = 5")
        assert row["role"] == "stats"
        invite = await db.fetchone("SELECT role FROM viewer_invites WHERE token = 'tok'")
        assert invite["role"] == "stats"
        await db.apply_schema()  # повторный запуск ничего не ломает
    finally:
        await db.close()


async def test_old_database_gets_click_columns(tmp_path):
    import sqlite3

    from bot.db import Database

    path = tmp_path / "old_click.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE users (
            tg_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            started_at INTEGER NOT NULL, source TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            is_subscribed INTEGER NOT NULL DEFAULT 0, sub_checked_at INTEGER,
            material_sent_at INTEGER, funnel_started_at INTEGER);
        INSERT INTO users(tg_id, started_at) VALUES (1, 0);
        CREATE TABLE funnel_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT, position INTEGER NOT NULL,
            delay_seconds INTEGER NOT NULL, requires_subscription INTEGER NOT NULL DEFAULT 0,
            on_unsub TEXT NOT NULL DEFAULT 'skip',
            text TEXT, media_id INTEGER, buttons_json TEXT,
            enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL);
        INSERT INTO funnel_steps(position, delay_seconds, text, created_at) VALUES (1, 0, 'x', 0);
        """
    )
    con.commit()
    con.close()

    db = await Database(path).connect()
    try:
        for table, column in [
            ("users", "lesson_clicked_at"),
            ("users", "deliver_claim_at"),
            ("users", "funnel_fast"),
            ("funnel_steps", "stop_on_click"),
            ("funnel_steps", "after_click_seconds"),
        ]:
            assert await db._has_column(table, column), f"{table}.{column}"
        row = await db.fetchone("SELECT funnel_fast FROM users WHERE tg_id = 1")
        assert row["funnel_fast"] == 0
        assert (await db.fetchone("SELECT stop_on_click FROM funnel_steps"))["stop_on_click"] == 0
        await db.apply_schema()  # повторный запуск ничего не ломает
    finally:
        await db.close()
