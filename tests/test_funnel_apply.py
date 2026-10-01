"""Новая воронка применяется ко всем активным людям, но получают они только то, чего ещё не получали.

Правило (решение владельца): неважно, когда человек нажал /start — замена воронки (импорт JSON)
и правка задержки действуют на всех; прогресс человека сохраняется по номеру шага; срок ближайшего
поста считается от его собственной цепочки (от последней отправки), без залпа."""
import json
import time

from bot.repo.funnel import NOT_SCHEDULED, FunnelRepo
from bot.repo.media import MediaRepo
from bot.repo.users import UsersRepo

WEEK = 7 * 86400


async def make_user(db, uid, *, lesson_at, status="active"):
    await UsersRepo(db).upsert(uid)
    await db.execute(
        "UPDATE users SET material_sent_at = ?, funnel_started_at = ?, started_at = ?, status = ? WHERE tg_id = ?",
        (lesson_at, lesson_at, lesson_at, status, uid),
    )


async def rows(db, uid):
    return {
        r["text"]: (r["status"], r["due_at"])
        for r in await db.fetchall(
            "SELECT fs.text, us.status, us.due_at FROM user_steps us "
            "JOIN funnel_steps fs ON fs.id = us.step_id WHERE us.user_id = ?", (uid,)
        )
    }


async def old_chain(db, funnel, now):
    """Старая воронка из двух постов; 1 — прошёл всё, 2 — получил только первый, 3 — урока ещё нет."""
    start = now - WEEK
    await make_user(db, 1, lesson_at=start)
    await make_user(db, 2, lesson_at=start)
    await UsersRepo(db).upsert(3)
    await funnel.add_step(1800, text="Старый 1", backfill=False)
    await funnel.add_step(7200, text="Старый 2", backfill=False)
    for uid in (1, 2):
        await funnel.enqueue(uid, now=start)
    await db.execute("UPDATE user_steps SET status='sent', sent_at=? WHERE user_id IN (1,2) "
                     "AND step_id=(SELECT id FROM funnel_steps WHERE position=1)", (start + 1800,))
    await db.execute("UPDATE user_steps SET status='sent', sent_at=? WHERE user_id=1 "
                     "AND step_id=(SELECT id FROM funnel_steps WHERE position=2)", (start + 9000,))
    await funnel.normalize_users([1, 2], start + 1800)


def new_chain_json(count=4):
    return json.dumps(
        [{"position": i, "delay_seconds": 3600 * i, "text": f"Новый {i}", "buttons": []} for i in range(1, count + 1)]
    )


async def test_import_gives_old_users_only_what_they_have_not_received(db):
    funnel, now = FunnelRepo(db), int(time.time())
    await old_chain(db, funnel, now)

    await funnel.import_json(new_chain_json(4), MediaRepo(db))

    done1, done2 = await rows(db, 1), await rows(db, 2)
    # получил 2 из 4 → ждёт 3 и 4
    assert [t for t, (st, _) in sorted(done1.items()) if st == "pending"] == ["Новый 3", "Новый 4"]
    # получил 1 из 4 → ждёт 2, 3, 4
    assert [t for t, (st, _) in sorted(done2.items()) if st == "pending"] == ["Новый 2", "Новый 3", "Новый 4"]
    # человек без урока ничего не получает от импорта — цепочку он получит вместе с уроком
    assert await rows(db, 3) == {}


async def test_import_timing_is_chain_relative_without_burst(db):
    """Срок есть только у ближайшего поста — «сейчас + его задержка»; остальные ждут своей очереди."""
    funnel, now = FunnelRepo(db), int(time.time())
    await old_chain(db, funnel, now)

    await funnel.import_json(new_chain_json(4), MediaRepo(db))

    done = await rows(db, 1)
    head_due = done["Новый 3"][1]
    assert now + 3 * 3600 - 5 <= head_due <= now + 3 * 3600 + 60      # от момента обновления, не из прошлого
    assert done["Новый 4"][1] == NOT_SCHEDULED


async def test_import_shorter_chain_leaves_finished_users_alone(db):
    funnel, now = FunnelRepo(db), int(time.time())
    await old_chain(db, funnel, now)

    await funnel.import_json(new_chain_json(2), MediaRepo(db))

    assert not [1 for st, _ in (await rows(db, 1)).values() if st == "pending"]


async def test_import_skips_blocked_users(db):
    funnel, now = FunnelRepo(db), int(time.time())
    await old_chain(db, funnel, now)
    await make_user(db, 4, lesson_at=now - WEEK, status="blocked")

    await funnel.import_json(new_chain_json(3), MediaRepo(db))

    assert await rows(db, 4) == {}


async def test_import_fast_mode_user_keeps_fast_timing(db):
    funnel, now = FunnelRepo(db), int(time.time())
    await old_chain(db, funnel, now)
    await db.execute("UPDATE users SET funnel_fast = 1 WHERE tg_id = 2")

    await funnel.import_json(new_chain_json(3), MediaRepo(db))

    assert (await rows(db, 2))["Новый 2"][1] <= now + 15


async def test_broken_import_keeps_everyones_progress(db):
    funnel, now = FunnelRepo(db), int(time.time())
    await old_chain(db, funnel, now)
    before = await rows(db, 2)

    try:
        await funnel.import_json("{}", MediaRepo(db))
    except ValueError:
        pass

    assert await rows(db, 2) == before


async def test_changing_delay_moves_waiting_users(db):
    """Поменяли задержку шага — у тех, кто его сейчас ждёт, срок пересчитывается от их последней отправки."""
    funnel, now = FunnelRepo(db), int(time.time())
    await make_user(db, 1, lesson_at=now - 3600)
    first = await funnel.add_step(1800, text="Первый", backfill=False)
    second = await funnel.add_step(7200, text="Второй", backfill=False)
    await funnel.enqueue(1, now=now - 3600)
    sent_at = now - 600                      # первый ушёл 10 минут назад
    await db.execute("UPDATE user_steps SET status='sent', sent_at=? WHERE step_id=?", (sent_at, first))
    await funnel.normalize_users([1], sent_at)

    await funnel.update_step(second, delay_seconds=3 * 3600)

    assert (await rows(db, 1))["Второй"][1] == sent_at + 3 * 3600


async def test_shortening_delay_never_sends_into_the_past_in_a_burst(db):
    funnel, now = FunnelRepo(db), int(time.time())
    await make_user(db, 1, lesson_at=now - 3600)
    first = await funnel.add_step(1800, text="Первый", backfill=False)
    second = await funnel.add_step(7200, text="Второй", backfill=False)
    await funnel.enqueue(1, now=now - 3600)
    await db.execute("UPDATE user_steps SET status='sent', sent_at=? WHERE step_id=?", (now - 3000, first))
    await funnel.normalize_users([1], now - 3000)

    await funnel.update_step(second, delay_seconds=60)       # срок уже «прошёл» → просто «сейчас»

    due = (await rows(db, 1))["Второй"][1]
    assert now - 2 <= due <= now + 5
