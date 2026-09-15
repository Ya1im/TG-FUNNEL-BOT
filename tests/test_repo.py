
from bot.repo.media import MediaRepo
from bot.repo.settings import SettingsRepo
from bot.repo.users import UsersRepo


async def test_upsert_user_keeps_source(db):
    users = UsersRepo(db)
    assert await users.upsert(1, "vasya", "Вася", source="ig_reels") is True
    assert await users.upsert(1, "vasya", "Вася", source=None) is False
    row = await users.get(1)
    assert row["source"] == "ig_reels"


async def test_blocked_user_returns_to_active_on_restart(db):
    users = UsersRepo(db)
    await users.upsert(1, "v", "В")
    await users.mark_blocked(1)
    await users.upsert(1, "v", "В")
    assert (await users.get(1))["status"] == "active"


async def test_mark_blocked_clears_queue(db):
    users = UsersRepo(db)
    await users.upsert(1, "v", "В")
    await db.execute(
        "INSERT INTO user_steps(user_id, step_id, due_at, status) VALUES(1, 7, 0, 'pending')"
    )
    await users.mark_blocked(1)
    row = await db.fetchone("SELECT status FROM user_steps WHERE user_id = 1")
    assert row["status"] == "skipped"
    assert (await users.get(1))["status"] == "blocked"


async def test_segments(db):
    users = UsersRepo(db)
    await users.upsert(1, source="a")
    await users.upsert(2, source="b")
    await users.upsert(3, source="a")
    await users.set_subscription(1, True)
    await users.mark_material_sent(1)
    await users.mark_blocked(3)

    assert sorted(await users.segment_ids("all")) == [1, 2]
    assert await users.segment_ids("subscribed") == [1]
    assert await users.segment_ids("not_subscribed") == [2]
    assert await users.segment_ids("got_material") == [1]
    assert await users.segment_ids("no_material") == [2]
    assert await users.segment_ids("blocked") == [3]
    assert await users.segment_ids("source", "a") == [1]


async def test_reset_clears_progress(db):
    users = UsersRepo(db)
    await users.upsert(1)
    await users.set_subscription(1, True)
    await users.mark_material_sent(1)
    await db.execute(
        "INSERT INTO user_steps(user_id, step_id, due_at) VALUES(1, 1, 0)"
    )
    await users.reset(1)
    row = await users.get(1)
    assert row["material_sent_at"] is None and row["is_subscribed"] == 0
    assert await db.fetchval("SELECT COUNT(*) FROM user_steps WHERE user_id = 1") == 0


async def test_stats_and_csv(db):
    users = UsersRepo(db)
    await users.upsert(1, "vasya", "Вася", source="ig")
    await users.set_subscription(1, True)
    stats = await users.stats()
    assert stats["total"] == 1 and stats["subscribed"] == 1
    csv_bytes = await users.export_csv()
    assert "vasya" in csv_bytes.decode("utf-8-sig")


async def test_settings_defaults_and_override(db):
    settings = SettingsRepo(db)
    assert await settings.get("btn_check") == "✅ Я подписан"
    await settings.set("btn_check", "Проверить")
    assert await settings.get("btn_check") == "Проверить"
    assert (await settings.all())["btn_check"] == "Проверить"


async def test_settings_get_int(db):
    settings = SettingsRepo(db)
    assert await settings.get_int("channel_id") is None
    await settings.set("channel_id", "-1001234567890")
    settings.invalidate()
    assert await settings.get_int("channel_id") == -1001234567890


async def test_media_slug_is_unique(db):
    media = MediaRepo(db)
    first = await media.save("krug", "video_note", "FILE_1")
    second = await media.save("krug", "video_note", "FILE_2")
    assert first == second
    assert await media.count() == 1
    assert (await media.get_by_slug("krug"))["file_id"] == "FILE_2"
