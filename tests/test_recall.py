"""Отзыв публикаций: журнал номеров сообщений и удаление у людей."""
from types import SimpleNamespace

from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import DeleteMessages

from bot.recall import build_plan, messages_per_step, run_recall
from bot.repo.funnel import FunnelRepo
from bot.repo.media import MediaRepo
from bot.repo.sentlog import SentLogRepo
from bot.repo.settings import SettingsRepo
from bot.repo.users import UsersRepo
from bot.scheduler import Scheduler

NOW = 1_000_000


class Bot:
    def __init__(self, probe_id=100, forbidden=()):
        self.deleted: list[tuple[int, list[int]]] = []
        self.sent: list[tuple[int, str]] = []
        self.probe_id = probe_id
        self.forbidden = set(forbidden)
        self._next = 500

    async def send_message(self, chat_id, text, reply_markup=None, disable_notification=None):
        if chat_id in self.forbidden:
            raise TelegramForbiddenError(method=DeleteMessages(chat_id=1, message_ids=[1]), message="blocked")
        self.sent.append((chat_id, text))
        self._next += 1
        # служебное сообщение получает «текущий» номер, обычные — свои
        return SimpleNamespace(message_id=self.probe_id if text == "⁠" else self._next)

    async def delete_messages(self, chat_id, message_ids):
        if chat_id in self.forbidden:
            raise TelegramForbiddenError(method=DeleteMessages(chat_id=1, message_ids=[1]), message="blocked")
        self.deleted.append((chat_id, sorted(message_ids)))
        return True


def make_deps(db):
    return SimpleNamespace(db=db, sent_log=SentLogRepo(db), limiter=None)


async def add_sent_step(db, user_id, step_id, sent_at, status="sent"):
    await db.execute(
        "INSERT OR IGNORE INTO users(tg_id, started_at) VALUES(?, 0)", (user_id,)
    )
    await db.execute(
        "INSERT INTO user_steps(user_id, step_id, due_at, status, sent_at) VALUES(?, ?, ?, ?, ?)",
        (user_id, step_id, sent_at, status, sent_at if status == "sent" else None),
    )


async def test_scheduler_records_message_ids(db):
    users, funnel, settings = UsersRepo(db), FunnelRepo(db), SettingsRepo(db)
    await users.upsert(1, "u", "В")
    await funnel.add_step(0, text="Пуш", backfill=False)
    await funnel.enqueue(1, now=0)
    bot = Bot()
    sent_log = SentLogRepo(db)

    class Gate:
        async def status(self, *a, **k):
            return "yes"

    sched = Scheduler(bot=bot, users=users, funnel=funnel, settings=settings, gate=Gate(),
                      sent_log=sent_log, now=lambda: NOW)
    await sched.tick()
    rows = await sent_log.since(0)
    assert [(r["user_id"], r["kind"]) for r in rows] == [(1, "step")]
    assert rows[0]["message_id"] > 500


async def test_exact_recall_deletes_logged_messages(db):
    deps = make_deps(db)
    await deps.sent_log.add(1, [10, 11], "step", 5, now=NOW - 60)
    await deps.sent_log.add(2, [20], "broadcast", 7, now=NOW - 60)
    await add_sent_step(db, 1, 5, NOW - 60)
    bot = Bot()
    result = await run_recall(bot, deps, since=NOW - 3600, now=NOW)
    assert (1, [10, 11]) in bot.deleted and (2, [20]) in bot.deleted
    assert result["exact"] == 3 and result["ranged"] == 0
    assert await deps.sent_log.since(0) == []
    row = await db.fetchone("SELECT status, last_error FROM user_steps WHERE user_id = 1")
    assert (row["status"], row["last_error"]) == ("skipped", "отозвано")
    assert bot.sent == []          # для точного отзыва служебные сообщения не нужны


async def test_legacy_recall_uses_probe_and_range(db):
    """Шаги отправлены до появления журнала: удаляем диапазон перед служебным сообщением."""
    deps = make_deps(db)
    funnel, media = FunnelRepo(db), MediaRepo(db)
    text_step = await funnel.add_step(0, text="Пост", backfill=False)
    note_id = await media.save("krug", "video_note", "F_NOTE")
    note_step = await funnel.add_step(0, text="Кружок с текстом", media_id=note_id, backfill=False)
    await add_sent_step(db, 1, text_step, NOW - 60)
    await add_sent_step(db, 1, note_step, NOW - 50)
    bot = Bot(probe_id=100)
    result = await run_recall(bot, deps, since=NOW - 3600, now=NOW)
    assert bot.sent == [(1, "⁠")]
    # 1 (пост) + 2 (кружок + текст) = 3 сообщения + сам служебный номер 100
    assert bot.deleted == [(1, [97, 98, 99, 100])]
    assert result["ranged"] == 3


async def test_recall_respects_window(db):
    deps = make_deps(db)
    funnel = FunnelRepo(db)
    step = await funnel.add_step(0, text="П", backfill=False)
    await add_sent_step(db, 1, step, NOW - 10 * 3600)         # старше окна
    bot = Bot()
    plan = await build_plan(deps, NOW - 3600)
    assert plan.is_empty
    await run_recall(bot, deps, since=NOW - 3600, now=NOW)
    assert bot.deleted == [] and bot.sent == []
    assert (await db.fetchone("SELECT status FROM user_steps"))["status"] == "sent"


async def test_recall_never_goes_past_48_hours(db):
    deps = make_deps(db)
    funnel = FunnelRepo(db)
    step = await funnel.add_step(0, text="П", backfill=False)
    await add_sent_step(db, 1, step, NOW - 50 * 3600)
    bot = Bot()
    await run_recall(bot, deps, since=NOW - 100 * 3600, now=NOW)
    assert bot.deleted == []


async def test_recall_cancels_overdue_pending_only(db):
    deps = make_deps(db)
    funnel = FunnelRepo(db)
    a = await funnel.add_step(0, text="А", backfill=False)
    b = await funnel.add_step(0, text="Б", backfill=False)
    await db.execute("INSERT INTO users(tg_id, started_at) VALUES(1, 0)")
    await db.execute("INSERT INTO user_steps(user_id, step_id, due_at, status) VALUES(1, ?, ?, 'pending')", (a, NOW - 5))
    await db.execute("INSERT INTO user_steps(user_id, step_id, due_at, status) VALUES(1, ?, ?, 'pending')", (b, NOW + 5000))
    result = await run_recall(Bot(), deps, since=NOW - 3600, now=NOW)
    rows = {r["step_id"]: r["status"] for r in await db.fetchall("SELECT step_id, status FROM user_steps")}
    assert rows == {a: "skipped", b: "pending"}
    assert result["cancelled"] == 1


async def test_recall_without_cancelling_pending(db):
    deps = make_deps(db)
    funnel = FunnelRepo(db)
    a = await funnel.add_step(0, text="А", backfill=False)
    await db.execute("INSERT INTO users(tg_id, started_at) VALUES(1, 0)")
    await db.execute("INSERT INTO user_steps(user_id, step_id, due_at, status) VALUES(1, ?, ?, 'pending')", (a, NOW - 5))
    await run_recall(Bot(), deps, since=NOW - 3600, cancel_pending=False, now=NOW)
    assert (await db.fetchone("SELECT status FROM user_steps"))["status"] == "pending"


async def test_recall_blocked_user_is_counted_not_fatal(db):
    deps = make_deps(db)
    await deps.sent_log.add(1, [10], "step", 5, now=NOW - 60)
    await deps.sent_log.add(2, [20], "step", 5, now=NOW - 60)
    bot = Bot(forbidden={1})
    result = await run_recall(bot, deps, since=NOW - 3600, now=NOW)
    assert result["blocked"] == 1 and result["users"] == 1
    assert (2, [20]) in bot.deleted


def test_messages_per_step():
    assert messages_per_step("текст", None, None) == 1
    assert messages_per_step("текст", "photo", "F") == 1
    assert messages_per_step("текст", "video_note", "F") == 2
    assert messages_per_step("я" * 2000, "photo", "F") == 2
