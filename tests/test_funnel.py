from bot.repo.funnel import FunnelRepo, human_delay, parse_delay
from bot.repo.media import MediaRepo
from bot.repo.users import UsersRepo


async def make_user(db, tg_id=1):
    await UsersRepo(db).upsert(tg_id, "u", "Вася")


async def test_enqueue_creates_all_enabled_steps(db):
    funnel = FunnelRepo(db)
    await make_user(db)
    await funnel.add_step(3600, text="Шаг 1")
    second = await funnel.add_step(7200, text="Шаг 2")
    third = await funnel.add_step(10800, text="Выключенный")
    await funnel.update_step(third, enabled=0)

    created = await funnel.enqueue(1, now=1000)
    assert created == 2
    rows = await db.fetchall("SELECT step_id, due_at FROM user_steps ORDER BY due_at")
    assert [r["due_at"] for r in rows] == [4600, 8200]
    assert rows[1]["step_id"] == second


async def test_enqueue_is_idempotent(db):
    funnel = FunnelRepo(db)
    await make_user(db)
    await funnel.add_step(60, text="Шаг")
    assert await funnel.enqueue(1) == 1
    assert await funnel.enqueue(1) == 0
    assert await db.fetchval("SELECT COUNT(*) FROM user_steps") == 1


async def test_fast_mode_uses_ten_second_steps(db):
    funnel = FunnelRepo(db)
    await make_user(db)
    await funnel.add_step(86400, text="Через сутки")
    await funnel.add_step(172800, text="Через двое")
    await funnel.enqueue(1, fast=True, now=0)
    rows = await db.fetchall("SELECT due_at FROM user_steps ORDER BY due_at")
    assert [r["due_at"] for r in rows] == [10, 20]


async def test_due_selection_respects_time_and_status(db):
    funnel = FunnelRepo(db)
    await make_user(db)
    await funnel.add_step(100, text="Ранний")
    await funnel.add_step(1000, text="Поздний")
    await funnel.enqueue(1, now=0)

    assert len(await funnel.due_steps(now=50)) == 0
    assert len(await funnel.due_steps(now=100)) == 1
    assert len(await funnel.due_steps(now=5000)) == 2

    queue_id = (await funnel.due_steps(now=5000))[0]["queue_id"]
    await funnel.mark_sent(queue_id)
    assert len(await funnel.due_steps(now=5000)) == 1


async def test_due_selection_skips_blocked_users(db):
    funnel = FunnelRepo(db)
    users = UsersRepo(db)
    await make_user(db)
    await funnel.add_step(0, text="Шаг")
    await funnel.enqueue(1, now=0)
    await users.set_status(1, "blocked")
    assert await funnel.due_steps(now=10) == []


async def test_due_row_carries_media_and_buttons(db):
    funnel, media = FunnelRepo(db), MediaRepo(db)
    await make_user(db)
    media_id = await media.save("krug", "video_note", "FILE_1")
    await funnel.add_step(0, text="Привет", media_id=media_id,
                          buttons=[{"text": "Канал", "url": "https://t.me/x"}])
    await funnel.enqueue(1, now=0)
    row = (await funnel.due_steps(now=10))[0]
    assert row["media_kind"] == "video_note" and row["media_file_id"] == "FILE_1"
    assert "Канал" in row["buttons_json"]


async def test_move_step_changes_order(db):
    funnel = FunnelRepo(db)
    first = await funnel.add_step(60, text="A")
    second = await funnel.add_step(120, text="B")
    await funnel.move_step(second, -1)
    order = [s["id"] for s in await funnel.list_steps()]
    assert order == [second, first]


async def test_export_import_roundtrip(db):
    funnel, media = FunnelRepo(db), MediaRepo(db)
    media_id = await media.save("krug", "video_note", "FILE_1")
    await funnel.add_step(3600, text="Раз", media_id=media_id, requires_subscription=True)
    await funnel.add_step(7200, text="Два")
    raw = await funnel.export_json()

    assert await funnel.import_json(raw, media) == 2
    steps = await funnel.list_steps()
    assert [s["text"] for s in steps] == ["Раз", "Два"]
    assert steps[0]["requires_subscription"] == 1
    assert steps[0]["media_slug"] == "krug"


async def test_pending_count_excludes_admin(db):
    funnel = FunnelRepo(db, admin_ids=(2,))
    await make_user(db, 1)
    await make_user(db, 2)
    await funnel.add_step(0, text="Шаг")
    await funnel.enqueue(1, now=0)
    await funnel.enqueue(2, now=0)
    assert await funnel.pending_count() == 1
    assert await funnel.pending_count(user_id=2) == 1


def test_parse_delay():
    assert parse_delay("30м") == 1800
    assert parse_delay("2ч") == 7200
    assert parse_delay("3д") == 259200
    assert parse_delay("1д 4ч") == 100800
    assert parse_delay("90") == 5400  # голое число — минуты
    assert parse_delay("1.5ч") == 5400
    assert parse_delay("завтра") is None


def test_human_delay():
    assert human_delay(30) == "30 сек"
    assert human_delay(1800) == "30 мин"
    assert human_delay(7200) == "2 ч"
    assert human_delay(259200) == "3 дн"


async def test_on_unsub_defaults_to_skip_and_roundtrips_through_export(db):
    from bot.repo.media import MediaRepo

    funnel = FunnelRepo(db)
    a = await funnel.add_step(0, text="a", requires_subscription=True)
    b = await funnel.add_step(0, text="b", requires_subscription=True, on_unsub="remind")
    assert (await funnel.get_step(a))["on_unsub"] == "skip"
    assert (await funnel.get_step(b))["on_unsub"] == "remind"

    raw = await funnel.export_json()
    await funnel.import_json(raw, MediaRepo(db))
    assert [s["on_unsub"] for s in await funnel.list_steps()] == ["skip", "remind"]


async def test_import_of_old_export_without_on_unsub_keeps_old_behaviour(db):
    import json

    from bot.repo.media import MediaRepo

    funnel = FunnelRepo(db)
    raw = json.dumps([{"delay_seconds": 0, "requires_subscription": True, "text": "x"}])
    await funnel.import_json(raw, MediaRepo(db))
    assert (await funnel.list_steps())[0]["on_unsub"] == "remind"
