"""Повторный /start в режиме «перезапуск воронки»: урок выдаётся заново и весь прогрев идёт с начала."""
import time

import pytest

from bot.config import Config
from bot.services import start_flow
from tests.test_start_flow import FakeBot, FakeUser, make_deps


@pytest.fixture
def config():
    return Config(bot_token="x", admin_ids=(99,), db_path=":memory:", messages_per_second=0, tick_seconds=60)


async def prepared(db, config, *, mode="restart", hours_ago=1):
    """Человек получил урок `hours_ago` часов назад и дошёл до середины прогрева."""
    deps = await make_deps(db, config)
    await deps.settings.set("repeat_start_mode", mode)
    await deps.material.add_block(text="Урок")
    await deps.repeat_start.add_block(text="Вы уже в деле")
    await deps.funnel.add_step(1800, text="Пост 1", backfill=False)
    await deps.funnel.add_step(7200, text="Пост 2", backfill=False)
    await deps.users.upsert(1, "vasya", "Вася", source="ig_reels")
    then = int(time.time()) - hours_ago * 3600
    await db.execute(
        "UPDATE users SET material_sent_at = ?, funnel_started_at = ?, is_subscribed = 1, invite_sent_at = ? WHERE tg_id = 1",
        (then, then, then),
    )
    await deps.funnel.enqueue(1, now=then)
    queue_id = (await deps.funnel.due_steps(now=then + 1800))[0]["queue_id"]
    await deps.funnel.mark_sent(queue_id, now=then + 1800)        # первый пост уже ушёл
    return deps


def texts(bot):
    return [c[1] for c in bot.calls if c[0] == "text"]


async def test_default_mode_is_message(db, config):
    deps = await make_deps(db, config)
    assert (await deps.settings.get("repeat_start_mode")) == "message"


async def test_restart_mode_delivers_lesson_again_and_restarts_whole_chain(db, config):
    deps = await prepared(db, config)
    bot = FakeBot()
    await start_flow(bot, deps, FakeUser(), chat_id=1)
    assert texts(bot) == ["Держи материал 🎁", "Урок"]             # сразу урок (с подводкой), без кружка/меню и без «Вы уже в деле»
    assert not any(c[0] == "invite" for c in bot.calls)             # личную ссылку в закрытый канал повторно не шлём
    rows = await db.fetchall(
        "SELECT us.status, us.due_at FROM user_steps us JOIN funnel_steps fs ON fs.id = us.step_id "
        "WHERE us.user_id = 1 ORDER BY fs.position")
    assert [r["status"] for r in rows] == ["pending", "pending"]    # пост 1 снова впереди, а не «отправлен»
    now = int(time.time())
    assert abs(rows[0]["due_at"] - (now + 1800)) < 5                # те же тайминги — от момента перезапуска
    user = await deps.users.get(1)
    assert abs(user["material_sent_at"] - now) < 5
    assert user["source"] == "ig_reels" and user["is_subscribed"] == 1    # источник и подписка сохранены


async def test_restart_mode_ignores_recent_repeat_press(db, config):
    """Двойной /start подряд не должен выдать урок дважды."""
    deps = await prepared(db, config, hours_ago=0)
    await db.execute("UPDATE users SET material_sent_at = ? WHERE tg_id = 1", (int(time.time()) - 10,))
    bot = FakeBot()
    await start_flow(bot, deps, FakeUser(), chat_id=1)
    assert bot.calls == []


async def test_restart_mode_does_not_touch_people_without_lesson(db, config):
    deps = await make_deps(db, config)
    await deps.settings.set("repeat_start_mode", "restart")
    await deps.material.add_block(text="Урок")
    bot = FakeBot()
    await start_flow(bot, deps, FakeUser(), chat_id=1)
    assert texts(bot) and "Урок" not in texts(bot)                  # новый человек — обычное меню, урока нет
    bot.calls.clear()
    await start_flow(bot, deps, FakeUser(), chat_id=1)
    assert "Урок" not in texts(bot)


async def test_message_mode_keeps_old_behaviour(db, config):
    deps = await prepared(db, config, mode="message")
    bot = FakeBot()
    await start_flow(bot, deps, FakeUser(), chat_id=1)
    assert texts(bot) == ["Вы уже в деле"]                          # как раньше: блоки «Повторный /start»
    statuses = [r["status"] for r in await db.fetchall("SELECT status FROM user_steps WHERE user_id = 1 ORDER BY id")]
    assert statuses == ["sent", "pending"]                          # воронка не тронута


async def test_users_restart_keeps_identity_and_test_mode(db, config):
    deps = await prepared(db, config)
    await db.execute("UPDATE users SET funnel_fast = 1 WHERE tg_id = 1")
    before = await deps.users.get(1)
    await deps.users.restart(1)
    after = await deps.users.get(1)
    assert after["material_sent_at"] is None and after["funnel_started_at"] is None
    assert after["deliver_claim_at"] is None and after["lesson_clicked_at"] is None
    assert after["started_at"] == before["started_at"] and after["source"] == "ig_reels"
    assert after["is_subscribed"] == 1 and after["funnel_fast"] == 1
    assert await deps.funnel.pending_count(1) == 0


async def test_restart_delivers_even_if_a_claim_was_left_over(db, config):
    deps = await prepared(db, config)
    await db.execute("UPDATE users SET deliver_claim_at = ? WHERE tg_id = 1", (int(time.time()) - 5,))
    bot = FakeBot()
    await start_flow(bot, deps, FakeUser(), chat_id=1)
    assert "Урок" in texts(bot)
