from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage

from bot.repo.funnel import FunnelRepo
from bot.repo.settings import SettingsRepo
from bot.repo.users import UsersRepo
from bot.scheduler import Scheduler

METHOD = SendMessage(chat_id=1, text="x")


class FakeBot:
    def __init__(self, fail_with=None):
        self.sent = []
        self.fail_with = fail_with

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.fail_with:
            raise self.fail_with
        self.sent.append((chat_id, text))
        return "ok"


class FakeGate:
    def __init__(self, subscribed=True):
        self.subscribed = subscribed
        self.calls = 0

    async def check(self, user_id):
        self.calls += 1
        return self.subscribed


async def build(db, bot=None, gate=None, now=1000):
    users, funnel, settings = UsersRepo(db), FunnelRepo(db), SettingsRepo(db)
    await users.upsert(1, "u", "Вася")
    bot = bot or FakeBot()
    scheduler = Scheduler(
        bot=bot,
        users=users,
        funnel=funnel,
        settings=settings,
        gate=gate or FakeGate(),
        now=lambda: now,
        sleep=_noop,
    )
    return scheduler, bot, users, funnel


async def _noop(_seconds):
    return None


async def test_tick_sends_due_step(db):
    scheduler, bot, _, funnel = await build(db)
    await funnel.add_step(0, text="Привет, {name}!")
    await funnel.enqueue(1, now=0)

    stats = await scheduler.tick()
    assert stats["sent"] == 1
    assert bot.sent == [(1, "Привет, Вася!")]
    assert (await db.fetchone("SELECT status FROM user_steps"))["status"] == "sent"


async def test_tick_ignores_future_steps(db):
    scheduler, bot, _, funnel = await build(db, now=100)
    await funnel.add_step(500, text="Потом")
    await funnel.enqueue(1, now=100)
    assert (await scheduler.tick())["sent"] == 0
    assert bot.sent == []


async def test_gated_step_postponed_when_unsubscribed(db):
    gate = FakeGate(subscribed=False)
    scheduler, bot, _, funnel = await build(db, gate=gate)
    await funnel.add_step(0, text="Закрытый инсайт", requires_subscription=True)
    await funnel.enqueue(1, now=0)

    stats = await scheduler.tick()
    assert stats["held"] == 1 and stats["sent"] == 0
    row = await db.fetchone("SELECT status, attempts, due_at FROM user_steps")
    assert row["status"] == "pending" and row["attempts"] == 1
    assert row["due_at"] > 1000  # перенесён примерно на 6 часов
    assert len(bot.sent) == 1 and "подписчиков" in bot.sent[0][1]


async def test_gated_step_skipped_after_three_reminders(db):
    gate = FakeGate(subscribed=False)
    scheduler, bot, _, funnel = await build(db)
    scheduler.gate = gate
    await funnel.add_step(0, text="Закрытый", requires_subscription=True)
    await funnel.enqueue(1, now=0)

    for _ in range(3):
        await db.execute("UPDATE user_steps SET due_at = 0")
        await scheduler.tick()
    assert len(bot.sent) == 3

    await db.execute("UPDATE user_steps SET due_at = 0")
    stats = await scheduler.tick()
    assert stats["skipped"] == 1
    assert (await db.fetchone("SELECT status FROM user_steps"))["status"] == "skipped"
    assert len(bot.sent) == 3  # четвёртого напоминания нет


async def test_gated_step_sends_when_subscription_returns(db):
    gate = FakeGate(subscribed=False)
    scheduler, bot, _, funnel = await build(db, gate=gate)
    await funnel.add_step(0, text="Закрытый", requires_subscription=True)
    await funnel.enqueue(1, now=0)
    await scheduler.tick()

    gate.subscribed = True
    await db.execute("UPDATE user_steps SET due_at = 0")
    stats = await scheduler.tick()
    assert stats["sent"] == 1
    assert bot.sent[-1][1] == "Закрытый"


async def test_one_reminder_per_user_per_tick(db):
    gate = FakeGate(subscribed=False)
    scheduler, bot, _, funnel = await build(db, gate=gate)
    await funnel.add_step(0, text="Раз", requires_subscription=True)
    await funnel.add_step(0, text="Два", requires_subscription=True)
    await funnel.enqueue(1, now=0)
    await scheduler.tick()
    assert len(bot.sent) == 1


async def test_blocked_user_removed_from_queue(db):
    bot = FakeBot(fail_with=TelegramForbiddenError(method=METHOD, message="bot was blocked"))
    scheduler, _, users, funnel = await build(db, bot=bot)
    await funnel.add_step(0, text="Раз")
    await funnel.add_step(0, text="Два")
    await funnel.enqueue(1, now=0)

    stats = await scheduler.tick()
    assert stats["blocked"] >= 1
    assert (await users.get(1))["status"] == "blocked"
    assert await funnel.pending_count(1) == 0


async def test_failed_step_retries_then_gives_up(db):
    bot = FakeBot(fail_with=TelegramBadRequest(method=METHOD, message="chat not found"))
    scheduler, _, _, funnel = await build(db, bot=bot)
    await funnel.add_step(0, text="Раз")
    await funnel.enqueue(1, now=0)

    for _ in range(2):
        await db.execute("UPDATE user_steps SET due_at = 0")
        await scheduler.tick()
    row = await db.fetchone("SELECT status, attempts FROM user_steps")
    assert row["status"] == "pending" and row["attempts"] == 2  # ещё ретраит

    await db.execute("UPDATE user_steps SET due_at = 0")
    await scheduler.tick()
    row = await db.fetchone("SELECT status, attempts FROM user_steps")
    assert row["status"] == "failed" and row["attempts"] == 3


async def test_tick_calls_hooks(db):
    calls = []
    scheduler, _, _, _ = await build(db)
    scheduler.broadcast_hook = lambda: _record(calls, "broadcast")
    scheduler.backup_hook = lambda: _record(calls, "backup")
    scheduler.stats_export_hook = lambda: _record(calls, "stats_export")
    await scheduler.tick()
    assert calls == ["broadcast", "backup", "stats_export"]


async def _record(calls, name):
    calls.append(name)
