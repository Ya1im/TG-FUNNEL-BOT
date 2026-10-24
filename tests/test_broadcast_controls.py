"""Управление рассылкой: остановить, удалить у получателей, предпросмотр, защита от двойного запуска."""
from __future__ import annotations

import asyncio
import importlib
import time
from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import DeleteMessages

from bot.broadcast import BroadcastEngine
from bot.recall import run_broadcast_recall
from bot.repo.broadcasts import BroadcastsRepo
from bot.repo.sentlog import SentLogRepo
from bot.repo.users import UsersRepo
from tests.test_broadcast import FakeBot, MESSAGES, setup
from tests.test_broadcast_album import _start_collecting, album_message
from tests.test_e2e import ADMIN_ID, feed, make_callback, make_message, stack  # noqa: F401

NOW = 1_000_000
METHOD = DeleteMessages(chat_id=1, message_ids=[1])


# --- движок: остановка действует сразу ----------------------------------------


class StoppingBot(FakeBot):
    """Админ нажимает «Остановить» сразу после первой отправки."""

    def __init__(self, repo, stop_after=1):
        super().__init__()
        self.repo = repo
        self.stop_after = stop_after
        self.broadcast_id = None

    async def copy_message(self, chat_id, from_chat_id, message_id):
        result = await super().copy_message(chat_id, from_chat_id, message_id)
        if len(self.copied) == self.stop_after:
            await self.repo.cancel(self.broadcast_id)
        return result


async def test_stop_takes_effect_between_recipients_not_after_the_batch(db):
    users, repo = await setup(db, count=5)
    bot = StoppingBot(repo)
    engine = BroadcastEngine(bot, users, repo)
    bot.broadcast_id, _ = await engine.prepare(99, "all", MESSAGES)

    stats = await engine.run(bot.broadcast_id)

    assert stats["sent"] == 1 and stats["pending"] == 4
    assert len(bot.copied) == 1
    assert (await repo.get(bot.broadcast_id))["status"] == "cancelled"


async def test_stop_during_the_last_send_is_not_overwritten_by_done(db):
    users, repo = await setup(db, count=1)
    bot = StoppingBot(repo)
    engine = BroadcastEngine(bot, users, repo)
    bot.broadcast_id, _ = await engine.prepare(99, "all", MESSAGES)

    await engine.run(bot.broadcast_id)

    assert (await repo.get(bot.broadcast_id))["status"] == "cancelled"


async def test_recalled_broadcast_is_never_sent_again(db):
    users, repo = await setup(db, count=2)
    bot = FakeBot()
    engine = BroadcastEngine(bot, users, repo)
    bid, _ = await engine.prepare(99, "all", MESSAGES)
    await repo.set_status(bid, "recalled")

    await engine.run(bid)

    assert bot.copied == []


async def test_only_one_start_claims_a_broadcast(db):
    _, repo = await setup(db, count=1)
    bid = await repo.create(1, "all", MESSAGES)

    assert await repo.claim_run(bid) is True
    assert await repo.claim_run(bid) is False
    assert (await repo.get(bid))["status"] == "running"


# --- отзыв рассылки по журналу -------------------------------------------------


class RecallBot:
    def __init__(self, forbidden=(), bad_request=()):
        self.deleted: list[tuple[int, list[int]]] = []
        self.forbidden = set(forbidden)
        self.bad_request = set(bad_request)

    async def delete_messages(self, chat_id, message_ids):
        if chat_id in self.forbidden:
            raise TelegramForbiddenError(method=METHOD, message="blocked")
        if chat_id in self.bad_request:
            raise TelegramBadRequest(method=METHOD, message="message to delete not found")
        self.deleted.append((chat_id, sorted(message_ids)))
        return True


async def _journal(db, rows):
    sent_log = SentLogRepo(db)
    for user_id, ids, kind, ref_id, sent_at in rows:
        await sent_log.add(user_id, ids, kind, ref_id, sent_at)
    return sent_log


async def test_recall_deletes_only_this_broadcast_messages(db):
    users, repo = await setup(db, count=2)
    bid = await repo.create(1, "all", MESSAGES)
    sent_log = await _journal(db, [
        (1, [10, 11, 12], "broadcast", bid, NOW - 60),
        (2, [20], "broadcast", bid, NOW - 60),
        (1, [5], "broadcast", bid + 1, NOW - 60),   # другая рассылка
        (1, [6], "step", bid, NOW - 60),            # пост прогрева с тем же номером ref_id
    ])
    deps = SimpleNamespace(db=db, sent_log=sent_log, limiter=None, broadcasts=repo)
    bot = RecallBot()

    result = await run_broadcast_recall(bot, deps, bid, now=NOW)

    assert sorted(bot.deleted) == [(1, [10, 11, 12]), (2, [20])]
    assert result["users"] == 2 and result["messages"] == 4 and result["failed"] == 0
    left = await sent_log.since(0)
    assert sorted((r["kind"], r["ref_id"]) for r in left) == [("broadcast", bid + 1), ("step", bid)]
    assert (await repo.get(bid))["status"] == "recalled"


async def test_recall_counts_blocked_and_keeps_going(db):
    users, repo = await setup(db, count=2)
    bid = await repo.create(1, "all", MESSAGES)
    sent_log = await _journal(db, [(1, [10], "broadcast", bid, NOW - 5), (2, [20], "broadcast", bid, NOW - 5)])
    deps = SimpleNamespace(db=db, sent_log=sent_log, limiter=None, broadcasts=repo)
    bot = RecallBot(forbidden={1})

    result = await run_broadcast_recall(bot, deps, bid, now=NOW)

    assert result["blocked"] == 1 and result["users"] == 1
    assert bot.deleted == [(2, [20])]


async def test_recall_skips_messages_older_than_48_hours_and_reports_them(db):
    users, repo = await setup(db, count=2)
    bid = await repo.create(1, "all", MESSAGES)
    sent_log = await _journal(db, [
        (1, [10], "broadcast", bid, NOW - 49 * 3600),
        (2, [20], "broadcast", bid, NOW - 60),
    ])
    deps = SimpleNamespace(db=db, sent_log=sent_log, limiter=None, broadcasts=repo)
    bot = RecallBot()

    result = await run_broadcast_recall(bot, deps, bid, now=NOW)

    assert bot.deleted == [(2, [20])]
    assert result["too_old"] == 1


async def test_recall_with_empty_journal_changes_nothing(db):
    users, repo = await setup(db, count=1)
    bid = await repo.create(1, "all", MESSAGES)
    await repo.set_status(bid, "done")
    deps = SimpleNamespace(db=db, sent_log=SentLogRepo(db), limiter=None, broadcasts=repo)

    result = await run_broadcast_recall(RecallBot(), deps, bid, now=NOW)

    assert result["messages"] == 0
    assert (await repo.get(bid))["status"] == "done"   # нечего было удалять — рассылку не помечаем «удалённой»


# --- кнопки в админке ------------------------------------------------------------


def _markup(session):
    shown = [r for r in session.requests if type(r).__name__ in ("EditMessageText", "SendMessage")]
    return shown[-1].reply_markup


def _cbs(session) -> list[str]:
    return [b.callback_data for row in _markup(session).inline_keyboard for b in row if b.callback_data]


def _text(session) -> str:
    shown = [r for r in session.requests if type(r).__name__ in ("EditMessageText", "SendMessage")]
    return shown[-1].text


async def _broadcast_with_status(deps, status, users=(1, 2)):
    for uid in users:
        await deps.users.upsert(uid)
    deps.engine.sent_log = deps.sent_log
    bid, _ = await deps.engine.prepare(ADMIN_ID, "all", [{"text": "Привет", "media_kind": None, "file_id": None, "buttons": []}])
    if status != "draft":
        await deps.broadcasts.set_status(bid, status)
    return bid


async def test_running_broadcast_card_offers_stop_and_hides_delete_and_recall(stack):
    dp, bot, session, deps = stack
    bid = await _broadcast_with_status(deps, "running")

    await feed(dp, bot, callback=make_callback(f"a:bc:o:{bid}", user_id=ADMIN_ID))

    cbs = _cbs(session)
    assert f"a:bc:x:{bid}" in cbs
    assert not any(c.startswith(("a:bc:rc:", "a:bc:go:", "a:bc:del:")) for c in cbs)


async def test_stop_button_cancels_and_card_stops_offering_it(stack):
    dp, bot, session, deps = stack
    bid = await _broadcast_with_status(deps, "running")

    await feed(dp, bot, callback=make_callback(f"a:bc:x:{bid}", user_id=ADMIN_ID))

    assert (await deps.broadcasts.get(bid))["status"] == "cancelled"
    assert f"a:bc:x:{bid}" not in _cbs(session)


async def test_stop_on_finished_broadcast_is_refused(stack):
    dp, bot, session, deps = stack
    bid = await _broadcast_with_status(deps, "done")
    session.requests.clear()

    await feed(dp, bot, callback=make_callback(f"a:bc:x:{bid}", user_id=ADMIN_ID))

    assert (await deps.broadcasts.get(bid))["status"] == "done"
    assert [c for c in session.calls("AnswerCallbackQuery") if c.show_alert]


async def test_finished_broadcast_card_offers_recall_only_when_something_was_sent(stack):
    dp, bot, session, deps = stack
    bid = await _broadcast_with_status(deps, "draft")
    await deps.engine.run(bid)

    await feed(dp, bot, callback=make_callback(f"a:bc:o:{bid}", user_id=ADMIN_ID))
    assert f"a:bc:rc:{bid}" in _cbs(session)

    empty = await _broadcast_with_status(deps, "done", users=())
    await deps.broadcasts.set_targets(empty, [])
    await feed(dp, bot, callback=make_callback(f"a:bc:o:{empty}", user_id=ADMIN_ID))
    assert f"a:bc:rc:{empty}" not in _cbs(session)


async def test_recall_from_admin_asks_confirmation_then_deletes_for_everyone(stack):
    dp, bot, session, deps = stack
    bid = await _broadcast_with_status(deps, "draft")
    await deps.engine.run(bid)
    session.requests.clear()

    await feed(dp, bot, callback=make_callback(f"a:bc:rc:{bid}", user_id=ADMIN_ID))
    assert session.calls("DeleteMessages") == []        # пока только вопрос
    assert f"a:bc:rcok:{bid}" in _cbs(session)
    assert "2" in _text(session)                         # сколько людей заденет

    await feed(dp, bot, callback=make_callback(f"a:bc:rcok:{bid}", user_id=ADMIN_ID))

    deleted = session.calls("DeleteMessages")
    assert sorted(d.chat_id for d in deleted) == [1, 2]
    assert (await deps.broadcasts.get(bid))["status"] == "recalled"
    await feed(dp, bot, callback=make_callback(f"a:bc:o:{bid}", user_id=ADMIN_ID))
    assert f"a:bc:rc:{bid}" not in _cbs(session)


async def test_recall_works_for_albums_all_messages_of_the_album_are_deleted(stack):
    dp, bot, session, deps = stack
    await deps.users.upsert(1)
    deps.engine.sent_log = deps.sent_log
    await _start_collecting(dp, bot)
    for message_id in (50, 51, 52):
        await feed(dp, bot, message=album_message(message_id, f"P{message_id}"))
    await feed(dp, bot, callback=make_callback("a:bc:done", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:bc:tosend", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:bc:seg:all", user_id=ADMIN_ID))
    bid = (await deps.broadcasts.recent(1))[0]["id"]
    await deps.engine.run(bid)
    session.requests.clear()

    await feed(dp, bot, callback=make_callback(f"a:bc:rcok:{bid}", user_id=ADMIN_ID))

    deleted = session.calls("DeleteMessages")
    assert len(deleted) == 1 and len(deleted[0].message_ids) == 3


async def test_recall_of_running_broadcast_is_refused_stop_it_first(stack):
    dp, bot, session, deps = stack
    bid = await _broadcast_with_status(deps, "running")
    session.requests.clear()

    await feed(dp, bot, callback=make_callback(f"a:bc:rcok:{bid}", user_id=ADMIN_ID))

    assert session.calls("DeleteMessages") == []
    assert [c for c in session.calls("AnswerCallbackQuery") if c.show_alert]


async def test_history_delete_button_is_renamed_so_it_is_not_confused_with_recall(stack):
    dp, bot, session, deps = stack
    bid = await _broadcast_with_status(deps, "done")

    await feed(dp, bot, callback=make_callback(f"a:bc:o:{bid}", user_id=ADMIN_ID))

    labels = [b.text for row in _markup(session).inline_keyboard for b in row if b.callback_data == f"a:bc:del:{bid}"]
    assert labels and "истори" in labels[0].lower()


async def test_second_press_on_send_does_not_start_a_second_run(stack):
    """Двойной клик по «Отправить сейчас» раньше запускал два прогона, и люди получали рассылку дважды."""
    dp, bot, session, deps = stack
    bid = await _broadcast_with_status(deps, "draft", users=(1,))
    session.requests.clear()

    await feed(dp, bot, callback=make_callback(f"a:bc:go:{bid}", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback(f"a:bc:go:{bid}", user_id=ADMIN_ID))
    module = importlib.import_module("bot.handlers.admin.broadcast")
    await asyncio.gather(*list(module._tasks))

    to_user = [m for m in session.calls("SendMessage") if m.chat_id == 1]
    assert len(to_user) == 1
    assert [c for c in session.calls("AnswerCallbackQuery") if c.show_alert]


async def test_progress_message_has_stop_button_and_final_one_has_recall(stack):
    dp, bot, session, deps = stack
    bid = await _broadcast_with_status(deps, "draft", users=(1,))
    session.requests.clear()

    await feed(dp, bot, callback=make_callback(f"a:bc:go:{bid}", user_id=ADMIN_ID))
    module = importlib.import_module("bot.handlers.admin.broadcast")
    await asyncio.gather(*list(module._tasks))

    started = [m for m in session.calls("SendMessage") if m.chat_id == ADMIN_ID][0]
    assert [b.callback_data for row in started.reply_markup.inline_keyboard for b in row] == [f"a:bc:x:{bid}"]
    # итог (в тестовой подмене Telegram правка сообщения недоступна, поэтому он приходит новым сообщением)
    assert f"a:bc:rc:{bid}" in _cbs(session)
    assert "завершена" in _text(session)


# --- предпросмотр ----------------------------------------------------------------


async def test_preview_of_whole_draft_shows_everything_in_order(stack):
    dp, bot, session, deps = stack
    await _start_collecting(dp, bot)
    await feed(dp, bot, message=make_message("Вступление", user_id=ADMIN_ID, message_id=40))
    await feed(dp, bot, message=album_message(41, "P1", caption="Альбом"))
    await feed(dp, bot, message=album_message(42, "P2"))
    await feed(dp, bot, message=make_message("Концовка", user_id=ADMIN_ID, message_id=43))
    await feed(dp, bot, callback=make_callback("a:bc:done", user_id=ADMIN_ID))
    assert "a:bc:dpall" in _cbs(session)
    session.requests.clear()

    await feed(dp, bot, callback=make_callback("a:bc:dpall", user_id=ADMIN_ID))

    names = [n for n in session.names() if n in ("SendMessage", "SendMediaGroup")]
    assert names[:3] == ["SendMessage", "SendMediaGroup", "SendMessage"]
    assert session.calls("SendMessage")[0].text == "Вступление"
    assert session.calls("SendMessage")[1].text == "Концовка"
    assert all(m.chat_id == ADMIN_ID for m in session.calls("SendMessage") + session.calls("SendMediaGroup"))


async def test_card_preview_of_saved_broadcast_still_works(stack):
    dp, bot, session, deps = stack
    bid = await _broadcast_with_status(deps, "draft")
    session.requests.clear()

    await feed(dp, bot, callback=make_callback(f"a:bc:p:{bid}", user_id=ADMIN_ID))

    assert [m.text for m in session.calls("SendMessage")] == ["Привет"]
