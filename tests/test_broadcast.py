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

    async def send_message(self, chat_id, text, reply_markup=None):
        self.copied.append((chat_id, "reminder", text))
        return "sent"

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


async def test_delete_removes_broadcast_and_targets(db):
    users, repo = await setup(db)
    engine = BroadcastEngine(FakeBot(), users, repo)
    keep, _ = await engine.prepare(99, "all", MESSAGES)
    drop, _ = await engine.prepare(99, "all", MESSAGES)

    assert await repo.delete(drop) is True

    assert await repo.get(drop) is None
    assert await repo.pending_targets(drop) == []
    assert await repo.get(keep) is not None
    assert await repo.pending_targets(keep) != []


async def test_delete_refuses_running_broadcast(db):
    users, repo = await setup(db)
    engine = BroadcastEngine(FakeBot(), users, repo)
    bid, _ = await engine.prepare(99, "all", MESSAGES)
    await repo.set_status(bid, "running")

    assert await repo.delete(bid) is False
    assert await repo.get(bid) is not None


class StubGate:
    def __init__(self, result):
        self.result = result  # {user_id: "yes"|"no"|"error"}

    async def status(self, user_id, cached_seconds=0):
        return self.result[user_id]


async def _sub_engine(db, sub_mode, result):
    from bot.repo.settings import SettingsRepo

    users, repo = await setup(db)
    bot = FakeBot()
    engine = BroadcastEngine(bot, users, repo, gate=StubGate(result), settings=SettingsRepo(db))
    bid, _ = await engine.prepare(99, "all", MESSAGES, sub_mode=sub_mode)
    return engine, bot, repo, bid


async def test_sub_mode_off_never_checks_subscription(db):
    engine, bot, repo, bid = await _sub_engine(db, "off", {1: "no", 2: "no", 3: "no"})
    stats = await engine.run(bid)
    assert stats["sent"] == 3 and stats["skipped"] == 0


async def test_sub_mode_skip_sends_only_to_subscribed(db):
    engine, bot, repo, bid = await _sub_engine(db, "skip", {1: "yes", 2: "no", 3: "yes"})
    stats = await engine.run(bid)
    assert stats["sent"] == 2 and stats["skipped"] == 1
    assert {c[0] for c in bot.copied} == {1, 3}


async def test_sub_mode_remind_sends_reminder_instead_of_content(db):
    engine, bot, repo, bid = await _sub_engine(db, "remind", {1: "yes", 2: "no", 3: "yes"})
    stats = await engine.run(bid)
    assert stats["sent"] == 2 and stats["reminded"] == 1
    assert [c for c in bot.copied if c[0] == 2 and c[1] == "reminder"]
    assert not [c for c in bot.copied if c[0] == 2 and c[1] == 99]


async def test_sub_check_error_is_failed_not_reminded(db):
    engine, bot, repo, bid = await _sub_engine(db, "remind", {1: "error", 2: "yes", 3: "yes"})
    stats = await engine.run(bid)
    assert stats["failed"] == 1 and stats["reminded"] == 0
    assert not [c for c in bot.copied if c[0] == 1]


async def test_set_sub_mode_rejects_garbage(db):
    _, repo = await setup(db)
    bid = await repo.create(1, "all", MESSAGES)
    await repo.set_sub_mode(bid, "spam")
    assert (await repo.get(bid))["sub_mode"] == "off"
    await repo.set_sub_mode(bid, "skip")
    assert (await repo.get(bid))["sub_mode"] == "skip"
