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


async def test_add_step_backfills_existing_users_from_their_own_material_time(db):
    users = UsersRepo(db)
    funnel = FunnelRepo(db)
    await users.upsert(1)
    await db.execute("UPDATE users SET material_sent_at = ? WHERE tg_id = 1", (1000 - 3 * 86400,))
    await users.upsert(2)
    await db.execute("UPDATE users SET material_sent_at = ? WHERE tg_id = 2", (1000 - 3600,))

    step_id = await funnel.add_step(delay_seconds=7200, text="Новый шаг")

    rows = {r["user_id"]: r["due_at"] for r in await db.fetchall(
        "SELECT user_id, due_at FROM user_steps WHERE step_id = ?", (step_id,)
    )}
    assert rows[1] == 1000 - 3 * 86400 + 7200   # у давнего due_at уже в прошлом
    assert rows[2] == 1000 - 3600 + 7200         # у недавнего — своя дата + задержка


async def test_add_step_does_not_backfill_user_without_material(db):
    users, funnel = UsersRepo(db), FunnelRepo(db)
    await users.upsert(1)  # material_sent_at ещё NULL
    step_id = await funnel.add_step(delay_seconds=3600, text="Шаг")
    assert await db.fetchall("SELECT 1 FROM user_steps WHERE step_id = ?", (step_id,)) == []


async def test_add_step_skips_blocked_users(db):
    users, funnel = UsersRepo(db), FunnelRepo(db)
    await users.upsert(1)
    await db.execute("UPDATE users SET material_sent_at = 1000 WHERE tg_id = 1")
    await users.mark_blocked(1)
    step_id = await funnel.add_step(delay_seconds=3600, text="Шаг")
    assert await db.fetchall("SELECT 1 FROM user_steps WHERE step_id = ?", (step_id,)) == []


async def test_backfill_step_skips_users_already_sent_a_later_position(db):
    """Позиция ниже уже существующего отправленного шага достижима только через add_step +
    move_step (позиции всегда возрастают при создании) — переупорядочивание бэкфилл не вызывает
    (см. Global Constraints), поэтому здесь конечное состояние после реордера собирается напрямую,
    а backfill_step вызывается так, как его вызвал бы add_step в момент создания на итоговой позиции."""
    users, funnel = UsersRepo(db), FunnelRepo(db)
    await users.upsert(1)
    await users.upsert(2)
    await db.execute("UPDATE users SET material_sent_at = 0 WHERE tg_id IN (1, 2)")
    later_id = await funnel.add_step(delay_seconds=100, text="Уже существующий шаг")
    await db.execute(
        "UPDATE user_steps SET status = 'sent' WHERE step_id = ? AND user_id = 1", (later_id,)
    )  # пользователь 1 уже получил этот шаг, пользователь 2 — ещё нет (pending)

    mid_id = await db.execute(
        "INSERT INTO funnel_steps(position, delay_seconds, requires_subscription, on_unsub, "
        "text, media_id, buttons_json, enabled, created_at) VALUES(0, 50, 0, 'skip', "
        "'Вставлен перед уже отправленным', NULL, '[]', 1, 0)"
    )  # позиция 0 — ниже later_id, как если бы шаг вставили и подняли его выше через move_step

    added = await funnel.backfill_step(mid_id, position=0, delay_seconds=50)

    got_mid = {r["user_id"] for r in await db.fetchall(
        "SELECT user_id FROM user_steps WHERE step_id = ?", (mid_id,)
    )}
    assert got_mid == {2}  # пользователю 1 «более ранний» шаг задним числом не пришёл
    assert added == 1


async def test_backfill_step_treats_skipped_later_step_as_not_ahead(db):
    users, funnel = UsersRepo(db), FunnelRepo(db)
    await users.upsert(1)
    await db.execute("UPDATE users SET material_sent_at = 0 WHERE tg_id = 1")
    later_id = await funnel.add_step(delay_seconds=100, text="Более поздний")
    await db.execute(
        "UPDATE user_steps SET status = 'skipped' WHERE step_id = ? AND user_id = 1", (later_id,)
    )

    mid_id = await db.execute(
        "INSERT INTO funnel_steps(position, delay_seconds, requires_subscription, on_unsub, "
        "text, media_id, buttons_json, enabled, created_at) VALUES(0, 50, 0, 'skip', "
        "'Вставленный пораньше', NULL, '[]', 1, 0)"
    )

    await funnel.backfill_step(mid_id, position=0, delay_seconds=50)

    got = await db.fetchall("SELECT 1 FROM user_steps WHERE step_id = ? AND user_id = 1", (mid_id,))
    assert got != []  # skipped — не «дальше», шаг всё равно ставится


async def test_enabling_disabled_step_triggers_backfill(db):
    users, funnel = UsersRepo(db), FunnelRepo(db)
    await users.upsert(1)
    await db.execute("UPDATE users SET material_sent_at = 1000 WHERE tg_id = 1")
    step_id = await funnel.add_step(delay_seconds=60, text="Шаг")
    await funnel.update_step(step_id, enabled=0)
    await db.execute("DELETE FROM user_steps WHERE step_id = ?", (step_id,))  # имитируем «выключен ещё до всех»

    await funnel.update_step(step_id, enabled=1)

    rows = await db.fetchall("SELECT 1 FROM user_steps WHERE step_id = ? AND user_id = 1", (step_id,))
    assert rows != []


async def test_disabling_step_does_not_backfill(db):
    users, funnel = UsersRepo(db), FunnelRepo(db)
    await users.upsert(1)
    await db.execute("UPDATE users SET material_sent_at = 1000 WHERE tg_id = 1")
    step_id = await funnel.add_step(delay_seconds=60, text="Шаг")
    await db.execute("DELETE FROM user_steps WHERE step_id = ?", (step_id,))

    await funnel.update_step(step_id, enabled=0)

    assert await db.fetchall("SELECT 1 FROM user_steps WHERE step_id = ?", (step_id,)) == []


async def test_editing_other_fields_does_not_backfill(db):
    users, funnel = UsersRepo(db), FunnelRepo(db)
    await users.upsert(1)
    await db.execute("UPDATE users SET material_sent_at = 1000 WHERE tg_id = 1")
    step_id = await funnel.add_step(delay_seconds=60, text="Шаг")
    await db.execute("DELETE FROM user_steps WHERE step_id = ?", (step_id,))

    await funnel.update_step(step_id, text="Другой текст", delay_seconds=120)

    assert await db.fetchall("SELECT 1 FROM user_steps WHERE step_id = ?", (step_id,)) == []


async def test_reenabling_already_enabled_step_is_noop_for_backfill(db):
    users, funnel = UsersRepo(db), FunnelRepo(db)
    await users.upsert(1)
    await db.execute("UPDATE users SET material_sent_at = 1000 WHERE tg_id = 1")
    step_id = await funnel.add_step(delay_seconds=60, text="Шаг")
    before = len(await db.fetchall("SELECT 1 FROM user_steps WHERE step_id = ?", (step_id,)))

    await funnel.update_step(step_id, enabled=1)  # уже включён

    after = len(await db.fetchall("SELECT 1 FROM user_steps WHERE step_id = ?", (step_id,)))
    assert after == before


async def test_import_json_does_not_backfill_existing_users(db):
    """import_json полностью пересобирает цепочку — это не «добавление нового шага», а массовая
    загрузка; бэкфилл здесь не должен срабатывать (иначе старым пользователям задним числом
    придёт вся цепочка сразу, а история отправок стёрта DELETE и не с чем сверяться)."""
    users, funnel = UsersRepo(db), FunnelRepo(db)
    await users.upsert(1)
    await db.execute("UPDATE users SET material_sent_at = 1000 WHERE tg_id = 1")
    await funnel.add_step(3600, text="Раз")
    raw = await funnel.export_json()

    await funnel.import_json(raw, MediaRepo(db))

    assert await db.fetchall("SELECT 1 FROM user_steps") == []


async def test_seed_style_bulk_add_does_not_backfill_existing_users(db):
    """add_step(..., backfill=False) — тот же режим массовой загрузки, что использует import_json
    и bot/seed.py при первом наполнении пустой воронки."""
    users, funnel = UsersRepo(db), FunnelRepo(db)
    await users.upsert(1)
    await db.execute("UPDATE users SET material_sent_at = 1000 WHERE tg_id = 1")

    await funnel.add_step(3600, text="Раз", backfill=False)

    assert await db.fetchall("SELECT 1 FROM user_steps") == []
