"""Предпрод-режим: публикации только тестовым аккаунтам."""
from types import SimpleNamespace

import pytest

from bot.config import Config
from bot.deps import Deps
from bot.preprod import allowed_user_ids, init_preprod
from bot.repo.funnel import FunnelRepo
from bot.repo.settings import SettingsRepo
from bot.repo.users import UsersRepo
from bot.scheduler import Scheduler
from bot.sender import RateLimiter
from bot.services import auto_deliver_due

T0 = 1_000_000


async def test_allowed_ids_off_means_everyone(db):
    settings = SettingsRepo(db)
    assert await allowed_user_ids(settings, (99,)) is None


async def test_allowed_ids_on_lists_test_accounts_and_admins(db):
    settings = SettingsRepo(db)
    await settings.set("preprod_mode", "1")
    await settings.set("preprod_user_ids", "5, 6;7")
    assert await allowed_user_ids(settings, (99,)) == {5, 6, 7, 99}


async def test_init_preprod_turns_on_once(db):
    settings = SettingsRepo(db)
    assert await init_preprod(settings) is True
    assert await settings.get("preprod_mode") == "1"
    await settings.set("preprod_mode", "0")          # владелец выпустил бота в продакшен
    assert await init_preprod(settings) is False     # повторный запуск не включает его снова
    assert await settings.get("preprod_mode") == "0"


async def test_due_steps_filtered_by_only_users(db):
    users, funnel = UsersRepo(db), FunnelRepo(db)
    for uid in (1, 2, 3):
        await users.upsert(uid, "u", "В")
    await funnel.add_step(0, text="П", backfill=False)
    for uid in (1, 2, 3):
        await funnel.enqueue(uid, now=0)
    assert {r["user_id"] for r in await funnel.due_steps(10)} == {1, 2, 3}
    assert {r["user_id"] for r in await funnel.due_steps(10, only_users={2})} == {2}
    assert await funnel.due_steps(10, only_users=set()) == []


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(chat_id)
        return "ok"


class FakeGate:
    async def status(self, *a, **k):
        return "yes"

    async def check(self, *a, **k):
        return True


async def test_scheduler_sends_only_to_test_accounts_in_preprod(db):
    users, funnel, settings = UsersRepo(db), FunnelRepo(db, admin_ids=(99,)), SettingsRepo(db)
    for uid in (1, 2, 99):
        await users.upsert(uid, "u", "В")
    await funnel.add_step(0, text="П", backfill=False)
    for uid in (1, 2, 99):
        await funnel.enqueue(uid, now=0)
    await settings.set("preprod_mode", "1")
    await settings.set("preprod_user_ids", "2")
    bot = FakeBot()
    sched = Scheduler(bot=bot, users=users, funnel=funnel, settings=settings, gate=FakeGate(), now=lambda: 100)
    await sched.tick()
    assert sorted(bot.sent) == [2, 99]
    # остальным ничего не «сгорело» — шаг ждёт своего часа
    pending = await db.fetchall("SELECT user_id FROM user_steps WHERE status = 'pending'")
    assert [r["user_id"] for r in pending] == [1]
    # выпустили в продакшен — очередь разбирается
    await settings.set("preprod_mode", "0")
    await sched.tick()
    assert sorted(bot.sent) == [1, 2, 99]


@pytest.fixture
def config():
    return Config(bot_token="x", admin_ids=(99,), db_path=":memory:", messages_per_second=0, tick_seconds=60)


async def test_auto_delivery_only_for_test_accounts_in_preprod(db, config):
    deps = Deps.build(config, db, bot=None)
    deps.gate = FakeGate()
    deps.limiter = RateLimiter(0)
    await deps.material.add_block(text="Урок")
    for uid in (1, 2):
        await deps.users.upsert(uid, "u", "В")
        await db.execute("UPDATE users SET started_at = ? WHERE tg_id = ?", (T0, uid))
    await deps.settings.set("preprod_mode", "1")
    await deps.settings.set("preprod_user_ids", "2")
    bot = FakeBot()
    assert await auto_deliver_due(bot, deps, now=T0 + 3600) == 1
    assert set(bot.sent) == {2}
    assert (await deps.users.get(1))["material_sent_at"] is None


async def test_broadcast_prepare_limited_to_test_accounts_in_preprod(db):
    from bot.broadcast import BroadcastEngine
    from bot.repo.broadcasts import BroadcastsRepo

    users, settings = UsersRepo(db, admin_ids=(99,)), SettingsRepo(db)
    for uid in (1, 2, 3):
        await users.upsert(uid, "u", "В")
    await settings.set("preprod_mode", "1")
    await settings.set("preprod_user_ids", "3")
    engine = BroadcastEngine(FakeBot(), users, BroadcastsRepo(db), settings=settings)
    bid, total = await engine.prepare(99, "all", [{"text": "Привет", "buttons": []}])
    assert total == 1
    assert await engine.broadcasts.pending_targets(bid, 10) == [3]


async def test_broadcast_run_skips_real_people_if_preprod_enabled_meanwhile(db):
    from bot.broadcast import BroadcastEngine
    from bot.repo.broadcasts import BroadcastsRepo

    users, settings = UsersRepo(db, admin_ids=(99,)), SettingsRepo(db)
    for uid in (1, 2):
        await users.upsert(uid, "u", "В")
    bot = FakeBot()
    engine = BroadcastEngine(bot, users, BroadcastsRepo(db), settings=settings)
    bid, total = await engine.prepare(99, "all", [{"text": "Привет", "buttons": []}])
    assert total == 2
    await settings.set("preprod_mode", "1")
    await settings.set("preprod_user_ids", "2")
    await engine.run(bid)
    assert bot.sent == [2]


async def test_broadcast_records_message_ids(db):
    from bot.broadcast import BroadcastEngine
    from bot.repo.broadcasts import BroadcastsRepo
    from bot.repo.sentlog import SentLogRepo

    users = UsersRepo(db)
    await users.upsert(1, "u", "В")

    class Bot:
        async def send_message(self, chat_id, text, reply_markup=None):
            return SimpleNamespace(message_id=321)

    sent_log = SentLogRepo(db)
    engine = BroadcastEngine(Bot(), users, BroadcastsRepo(db), sent_log=sent_log)
    bid, _ = await engine.prepare(99, "all", [{"text": "Привет", "buttons": []}])
    await engine.run(bid)
    rows = await sent_log.since(0)
    assert [(r["user_id"], r["message_id"], r["kind"], r["ref_id"]) for r in rows] == [(1, 321, "broadcast", bid)]
