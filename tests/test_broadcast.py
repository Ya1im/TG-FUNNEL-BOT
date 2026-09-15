from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import CopyMessage

from bot.broadcast import BroadcastEngine
from bot.repo.broadcasts import BroadcastsRepo
from bot.repo.users import UsersRepo

METHOD = CopyMessage(chat_id=1, from_chat_id=1, message_id=1)
MESSAGES = [{"chat_id": 99, "message_id": 5}]


class FakeBot:
    def __init__(self, fail_for=None, error=None):
        self.copied = []
        self.fail_for = fail_for or set()
        self.error = error or TelegramForbiddenError(method=METHOD, message="bot was blocked")

    async def copy_message(self, chat_id, from_chat_id, message_id):
        if chat_id in self.fail_for:
            raise self.error
        self.copied.append((chat_id, from_chat_id, message_id))
        return "copied"


async def setup(db, count=3):
    users = UsersRepo(db)
    for uid in range(1, count + 1):
        await users.upsert(uid)
    return users, BroadcastsRepo(db)


async def test_targets_are_snapshot_by_segment(db):
    users, repo = await setup(db)
    await users.set_subscription(2, True)
    engine = BroadcastEngine(FakeBot(), users, repo)

    broadcast_id, total = await engine.prepare(99, "subscribed", MESSAGES)
    assert total == 1
    # новый подписчик после создания рассылки в неё уже не попадёт
    await users.set_subscription(3, True)
    assert await repo.pending_targets(broadcast_id) == [2]


async def test_run_sends_to_everyone(db):
    users, repo = await setup(db)
    bot = FakeBot()
    engine = BroadcastEngine(bot, users, repo)
    broadcast_id, _ = await engine.prepare(99, "all", MESSAGES)

    stats = await engine.run(broadcast_id)
    assert stats["sent"] == 3 and stats["pending"] == 0
    assert sorted(c[0] for c in bot.copied) == [1, 2, 3]
    assert (await repo.get(broadcast_id))["status"] == "done"


async def test_blocked_is_counted_separately(db):
    users, repo = await setup(db)
    bot = FakeBot(fail_for={2})
    engine = BroadcastEngine(bot, users, repo)
    broadcast_id, _ = await engine.prepare(99, "all", MESSAGES)

    stats = await engine.run(broadcast_id)
    assert stats["sent"] == 2 and stats["blocked"] == 1 and stats["failed"] == 0
    assert (await users.get(2))["status"] == "blocked"


async def test_failure_is_counted_as_failed(db):
    users, repo = await setup(db)
    bot = FakeBot(fail_for={3}, error=TelegramBadRequest(method=METHOD, message="chat not found"))
    engine = BroadcastEngine(bot, users, repo)
    broadcast_id, _ = await engine.prepare(99, "all", MESSAGES)

    stats = await engine.run(broadcast_id)
    assert stats["failed"] == 1 and stats["sent"] == 2


async def test_resume_after_restart_sends_only_rest(db):
    users, repo = await setup(db)
    bot = FakeBot()
    engine = BroadcastEngine(bot, users, repo)
    broadcast_id, _ = await engine.prepare(99, "all", MESSAGES)
    await repo.mark_target(broadcast_id, 1, "sent")
    await repo.set_status(broadcast_id, "running")

    await engine.resume_unfinished()
    assert sorted(c[0] for c in bot.copied) == [2, 3]
    assert (await repo.stats(broadcast_id))["sent"] == 3


async def test_multiple_messages_are_copied_in_order(db):
    users, repo = await setup(db, count=1)
    bot = FakeBot()
    engine = BroadcastEngine(bot, users, repo)
    messages = [{"chat_id": 99, "message_id": 5}, {"chat_id": 99, "message_id": 6}]
    broadcast_id, _ = await engine.prepare(99, "all", messages)
    await engine.run(broadcast_id)
    assert [c[2] for c in bot.copied] == [5, 6]


async def test_scheduled_runs_when_due(db):
    users, repo = await setup(db, count=1)
    bot = FakeBot()
    engine = BroadcastEngine(bot, users, repo, now=lambda: 5000)
    broadcast_id, _ = await engine.prepare(99, "all", MESSAGES, scheduled_at=4000)

    await engine.run_scheduled()
    assert (await repo.get(broadcast_id))["status"] == "done"
    assert bot.copied


async def test_scheduled_waits_for_its_time(db):
    users, repo = await setup(db, count=1)
    bot = FakeBot()
    engine = BroadcastEngine(bot, users, repo, now=lambda: 1000)
    await engine.prepare(99, "all", MESSAGES, scheduled_at=4000)
    await engine.run_scheduled()
    assert bot.copied == []


async def test_progress_callback_receives_stats(db):
    users, repo = await setup(db, count=2)
    seen = []
    engine = BroadcastEngine(FakeBot(), users, repo, now=lambda: 0)
    broadcast_id, _ = await engine.prepare(99, "all", MESSAGES)
    await engine.run(broadcast_id, progress=lambda stats: _collect(seen, stats))
    assert seen and seen[-1]["sent"] == 2


async def _collect(seen, stats):
    seen.append(stats)
