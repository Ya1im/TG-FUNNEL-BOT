"""Шаг прогрева из нескольких сообщений: хранение, отправка, отзыв, экспорт."""
import json
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage

from bot.db import Database
from bot.recall import build_plan
from bot.repo.funnel import FunnelRepo, blocks_from_row, step_messages
from bot.repo.media import MediaRepo
from bot.repo.sentlog import SentLogRepo
from bot.repo.settings import SettingsRepo
from bot.repo.users import UsersRepo
from bot.scheduler import STEP_MESSAGE_PAUSE, Scheduler

METHOD = SendMessage(chat_id=1, text="x")


class Bot:
    """fail_at — номер (с 1) вызова отправки, который упадёт с заданной ошибкой."""

    def __init__(self, fail_at=None, error=None):
        self.calls = []
        self.fail_at = fail_at
        self.error = error
        self._id = 100

    def _next(self, kind, chat_id, payload):
        self.calls.append((kind, chat_id, payload))
        if self.fail_at == len(self.calls):
            raise self.error
        self._id += 1
        return SimpleNamespace(message_id=self._id)

    async def send_message(self, chat_id, text, reply_markup=None, disable_notification=None):
        return self._next("text", chat_id, text)

    async def send_photo(self, chat_id, file_id, caption=None, reply_markup=None):
        return self._next("photo", chat_id, (file_id, caption))

    async def send_video_note(self, chat_id, file_id, reply_markup=None):
        return self._next("note", chat_id, file_id)


class Gate:
    async def status(self, *a, **k):
        return "yes"


def item(text=None, kind=None, file_id=None, buttons=None):
    return {"text": text, "media_kind": kind, "file_id": file_id, "buttons": buttons or []}


async def setup(db, bot=None, *msgs, delay=100, later=()):
    users = UsersRepo(db)
    await users.upsert(1, "u", "В")
    await db.execute("UPDATE users SET material_sent_at = 0 WHERE tg_id = 1")
    funnel = FunnelRepo(db)
    step_id = await funnel.add_step(delay, text=msgs[0]["text"], backfill=False, extra_messages=list(msgs[1:]))
    for later_delay in later:
        await funnel.add_step(later_delay, text="Следующий", backfill=False)
    await funnel.enqueue(1, now=0)
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    sched = Scheduler(
        bot=bot or Bot(), users=users, funnel=funnel, settings=SettingsRepo(db), gate=Gate(),
        sent_log=SentLogRepo(db), now=lambda: 1000, sleep=fake_sleep,
    )
    return funnel, sched, step_id, sleeps


async def test_step_sends_all_messages_in_order_with_pause(db):
    bot = Bot()
    funnel, sched, step_id, sleeps = await setup(
        db, bot, item("Первое"), item("Второе", "photo", "PH"), item(None, "video_note", "VN")
    )
    assert (await sched.tick())["sent"] == 1
    assert [c[0] for c in bot.calls] == ["text", "photo", "note"]
    assert bot.calls[1][2] == ("PH", "Второе")
    assert sleeps == [STEP_MESSAGE_PAUSE, STEP_MESSAGE_PAUSE]      # между сообщениями, не после последнего
    # журнал для отзыва содержит все три сообщения
    logged = await db.fetchall("SELECT message_id FROM sent_messages WHERE kind = 'step' AND ref_id = ?", (step_id,))
    assert sorted(r["message_id"] for r in logged) == [101, 102, 103]


async def test_next_step_counts_from_the_last_message_of_step(db):
    bot = Bot()
    funnel, sched, step_id, _ = await setup(db, bot, item("A"), item("B"), later=(500,))
    await sched.tick()
    rows = await db.fetchall(
        "SELECT us.step_id, us.due_at, us.status FROM user_steps us WHERE us.user_id = 1 ORDER BY us.step_id")
    assert [(r["status"]) for r in rows] == ["sent", "pending"]
    assert rows[1]["due_at"] == 1000 + 500


async def test_failure_on_second_message_marks_step_sent_without_duplicates(db):
    err = TelegramBadRequest(method=METHOD, message="Bad Request: wrong file identifier")
    bot = Bot(fail_at=2, error=err)
    funnel, sched, step_id, _ = await setup(db, bot, item("A"), item("B"), item("C"))
    stats = await sched.tick()
    assert stats["sent"] == 1 and stats["failed"] == 0
    assert len(bot.calls) == 2                                    # третье не отправляли
    assert (await sched.tick())["sent"] == 0                      # и шаг не повторяется
    logged = await db.fetchall("SELECT message_id FROM sent_messages WHERE ref_id = ?", (step_id,))
    assert [r["message_id"] for r in logged] == [101]


async def test_failure_on_first_message_retries_whole_step(db):
    err = TelegramBadRequest(method=METHOD, message="Bad Request: oops")
    bot = Bot(fail_at=1, error=err)
    funnel, sched, step_id, _ = await setup(db, bot, item("A"), item("B"))
    stats = await sched.tick()
    assert stats["failed"] == 1 and stats["sent"] == 0
    assert len(bot.calls) == 1
    row = await db.fetchone("SELECT status, attempts FROM user_steps WHERE user_id = 1")
    assert row["status"] == "pending" and row["attempts"] == 1


async def test_blocked_in_the_middle_stops_and_clears_queue(db):
    err = TelegramForbiddenError(method=METHOD, message="Forbidden: bot was blocked by the user")
    bot = Bot(fail_at=2, error=err)
    funnel, sched, step_id, _ = await setup(db, bot, item("A"), item("B"), item("C"))
    stats = await sched.tick()
    assert stats["blocked"] == 1 and len(bot.calls) == 2
    assert await funnel.pending_count(1) == 0
    assert (await UsersRepo(db).get(1))["status"] == "blocked"


async def test_single_message_step_works_as_before(db):
    bot = Bot()
    funnel, sched, step_id, sleeps = await setup(db, bot, item("Один"))
    assert (await sched.tick())["sent"] == 1
    assert len(bot.calls) == 1 and sleeps == []


async def test_set_messages_mirrors_first_into_columns_and_rest_into_extras(db):
    funnel = FunnelRepo(db)
    media = MediaRepo(db)
    media_id = await media.save("m1", "photo", "PH1", "u1")
    step_id = await funnel.add_step(100, text="Старое", backfill=False)
    await funnel.set_messages(step_id, [
        {**item("Первое", "photo", "PH1", [{"text": "Ссылка", "url": "https://x.ru"}]), "media_id": media_id},
        item("Второе", buttons=[{"text": "Б", "url": "https://y.ru"}]),
    ])
    row = next(s for s in await funnel.list_steps() if s["id"] == step_id)
    assert row["text"] == "Первое" and row["media_id"] == media_id
    msgs = step_messages(row)
    assert [m["text"] for m in msgs] == ["Первое", "Второе"]
    assert msgs[0]["file_id"] == "PH1" and msgs[0]["buttons"][0]["text"] == "Ссылка"
    assert msgs[1]["buttons"][0]["url"] == "https://y.ru"
    assert [b.text for b in blocks_from_row(row)] == ["Первое", "Второе"]


async def test_set_messages_rejects_empty_list(db):
    funnel = FunnelRepo(db)
    step_id = await funnel.add_step(100, text="X", backfill=False)
    with pytest.raises(ValueError):
        await funnel.set_messages(step_id, [])


async def test_old_step_without_extras_has_one_message(db):
    funnel = FunnelRepo(db)
    step_id = await funnel.add_step(100, text="Старый", backfill=False)
    row = next(s for s in await funnel.list_steps() if s["id"] == step_id)
    assert [m["text"] for m in step_messages(row)] == ["Старый"]


async def test_old_database_gets_extra_column_on_connect(tmp_path):
    path = tmp_path / "old.db"
    database = await Database(path).connect()
    await database.execute("ALTER TABLE funnel_steps DROP COLUMN extra_messages_json")
    await database.close()
    database = await Database(path).connect()
    try:
        funnel = FunnelRepo(database)
        step_id = await funnel.add_step(100, text="X", backfill=False, extra_messages=[item("Y")])
        row = next(s for s in await funnel.list_steps() if s["id"] == step_id)
        assert len(step_messages(row)) == 2
    finally:
        await database.close()


async def test_export_import_round_trip_keeps_extra_messages(db):
    funnel = FunnelRepo(db)
    media = MediaRepo(db)
    media_id = await media.save("pic", "photo", "PH", "u")
    await funnel.add_step(
        100, text="Первое", backfill=False,
        extra_messages=[{**item("Второе", "photo", "PH", [{"text": "К", "url": "https://z.ru"}]), "media_id": media_id}],
    )
    raw = await funnel.export_json()
    exported = json.loads(raw)
    assert exported[0]["extra_messages"][0]["media_slug"] == "pic"
    await funnel.import_json(raw, media)
    row = (await funnel.list_steps())[0]
    msgs = step_messages(row)
    assert [m["text"] for m in msgs] == ["Первое", "Второе"]
    assert msgs[1]["file_id"] == "PH" and msgs[1]["buttons"][0]["url"] == "https://z.ru"


async def test_legacy_recall_counts_every_message_of_step(db):
    funnel = FunnelRepo(db)
    await UsersRepo(db).upsert(1, "u", "В")
    step_id = await funnel.add_step(
        100, text="A", backfill=False, extra_messages=[item("B"), item(None, "video_note", "VN")]
    )
    await funnel.enqueue(1, now=0)
    queue_id = (await funnel.due_steps(now=100))[0]["queue_id"]
    await funnel.mark_sent(queue_id, now=100)
    deps = SimpleNamespace(db=db, sent_log=SentLogRepo(db), limiter=None)
    plan = await build_plan(deps, since=0)
    assert plan.users[1].legacy_messages == 3 and plan.users[1].legacy_steps == 1
