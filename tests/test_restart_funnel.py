"""Повторный /start в режиме «перезапуск воронки»: урок выдаётся заново и весь прогрев идёт с начала."""
import time

import pytest

from bot.config import Config
from bot.services import check_subscription_flow, start_flow
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


async def test_restart_mode_starts_funnel_from_scratch_with_circle_and_subscription_check(db, config):
    deps = await prepared(db, config)
    note_id = await deps.media.save("krug", "video_note", "FILE_NOTE", "u1")
    await deps.settings.set("welcome_note_media_id", str(note_id))
    await deps.settings.set("channel_url", "https://t.me/mychannel")
    bot = FakeBot()
    await start_flow(bot, deps, FakeUser(), chat_id=1)
    # как у нового человека: кружок и меню с кнопками подписки; урока и «Вы уже в деле» нет
    assert [c[0] for c in bot.calls] == ["video_note", "text"]
    assert bot.calls[1][2].inline_keyboard[1][0].callback_data == "check_sub"
    assert "Урок" not in texts(bot) and "Вы уже в деле" not in texts(bot)
    assert not any(c[0] == "invite" for c in bot.calls)
    # прежний прогресс полностью сброшен
    user = await deps.users.get(1)
    assert user["material_sent_at"] is None and user["funnel_started_at"] is None
    assert user["is_subscribed"] == 0 and user["invite_sent_at"] is None
    assert abs(user["started_at"] - int(time.time())) < 5           # таймер автовыдачи идёт от перезапуска
    assert user["source"] == "ig_reels"                             # источник сохранён
    assert await deps.funnel.pending_count(1) == 0


async def test_after_restart_subscription_check_delivers_lesson_and_chain_starts_over(db, config):
    deps = await prepared(db, config)
    await deps.settings.set("private_channel_id", "-100123")
    bot = FakeBot()
    await start_flow(bot, deps, FakeUser(), chat_id=1)
    bot.calls.clear()
    assert await check_subscription_flow(bot, deps, 1, 1) is True
    assert "Урок" in texts(bot) and any(c[0] == "invite" for c in bot.calls)   # личная ссылка выдаётся заново
    rows = await db.fetchall(
        "SELECT us.status, us.due_at FROM user_steps us JOIN funnel_steps fs ON fs.id = us.step_id "
        "WHERE us.user_id = 1 ORDER BY fs.position")
    assert [r["status"] for r in rows] == ["pending", "pending"]    # пост 1 снова первый, а не «отправлен»
    now = int(time.time())
    assert abs(rows[0]["due_at"] - (now + 1800)) < 5                # те же тайминги — от новой выдачи урока
    assert rows[1]["due_at"] > now + 10**8                          # пост 2 ждёт отправки поста 1 (цепочка)


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


async def test_users_restart_resets_progress_but_keeps_identity_and_test_mode(db, config):
    deps = await prepared(db, config, hours_ago=5)
    await db.execute(
        "UPDATE users SET funnel_fast = 1, deliver_claim_at = 5, sub_checked_at = 5, started_at = started_at - 1000 WHERE tg_id = 1")
    before = await deps.users.get(1)
    await deps.users.restart(1)
    after = await deps.users.get(1)
    assert after["material_sent_at"] is None and after["funnel_started_at"] is None
    assert after["deliver_claim_at"] is None and after["lesson_clicked_at"] is None
    assert after["is_subscribed"] == 0 and after["sub_checked_at"] is None and after["invite_sent_at"] is None
    assert after["started_at"] > before["started_at"]
    assert after["source"] == "ig_reels" and after["funnel_fast"] == 1
    assert await deps.funnel.pending_count(1) == 0


async def test_leftover_delivery_claim_does_not_block_delivery_after_restart(db, config):
    deps = await prepared(db, config)
    await db.execute("UPDATE users SET deliver_claim_at = ? WHERE tg_id = 1", (int(time.time()) - 5,))
    bot = FakeBot()
    await start_flow(bot, deps, FakeUser(), chat_id=1)
    bot.calls.clear()
    assert await check_subscription_flow(bot, deps, 1, 1) is True
    assert "Урок" in texts(bot)
