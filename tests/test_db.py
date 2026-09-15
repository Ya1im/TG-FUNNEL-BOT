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
