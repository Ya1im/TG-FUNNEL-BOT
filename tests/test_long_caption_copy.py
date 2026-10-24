"""Длинная подпись уходит одним постом: копией исходного сообщения админа, а не двумя сообщениями."""
from datetime import datetime, timezone

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import CopyMessage, SendPhoto
from aiogram.types import Chat, Message, MessageId

from bot import content
from bot.content import ContentBlock, visible_len
from bot.handlers.admin.common import preview_notes
from bot.sender import send_block
from tests.test_broadcast_album import album_message
from tests.test_e2e import ADMIN_ID, feed, make_callback, make_message, stack  # noqa: F401

BTN = [{"text": "Смотреть", "url": "https://x.ru"}]
LONG = "я" * 1500


def too_long(method):
    return TelegramBadRequest(method=method, message="Bad Request: message caption is too long")


class Bot:
    """hard_limit — сколько знаков подписи принимает sendPhoto; copy_limit — сколько принимает copyMessage."""

    def __init__(self, hard_limit=1024, copy_limit=None, copy_error=None):
        self.sent = []
        self.hard_limit = hard_limit
        self.copy_limit = copy_limit
        self.copy_error = copy_error
        self._id = 0

    def _message(self, text=None):
        self._id += 1
        return Message(message_id=self._id, date=datetime.now(timezone.utc), chat=Chat(id=1, type="private"), text=text)

    async def send_photo(self, chat_id, file_id, caption=None, reply_markup=None):
        if caption and visible_len(caption) > self.hard_limit:
            raise too_long(SendPhoto(chat_id=1, photo="x"))
        self.sent.append(("photo", caption, reply_markup is not None))
        return self._message(caption)

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(("text", text, reply_markup is not None))
        return self._message(text)

    async def copy_message(self, chat_id, from_chat_id, message_id, reply_markup=None):
        if self.copy_error:
            raise TelegramBadRequest(method=CopyMessage(chat_id=1, from_chat_id=1, message_id=1), message=self.copy_error)
        if self.copy_limit is not None and self.copy_limit < 2000:
            raise too_long(CopyMessage(chat_id=1, from_chat_id=1, message_id=1))
        self.sent.append(("copy", chat_id, from_chat_id, message_id, reply_markup is not None))
        self._id += 1
        return MessageId(message_id=self._id)

    async def copy_messages(self, chat_id, from_chat_id, message_ids):
        self.sent.append(("copies", chat_id, from_chat_id, list(message_ids)))
        result = []
        for _ in message_ids:
            self._id += 1
            result.append(MessageId(message_id=self._id))
        return result


def kinds(bot):
    return [s[0] for s in bot.sent]


def block(text=LONG, source=(77, [5]), buttons=None, **kw):
    return ContentBlock(text, "photo", "P", buttons or [], source_chat=source[0], source_ids=source[1], **kw)


async def test_long_caption_goes_as_a_copy_of_the_original_when_telegram_refuses_the_caption():
    bot = Bot(hard_limit=1024)
    outcome = await send_block(block(buttons=BTN), bot, 9)
    assert outcome.ok and outcome.caption_split is False and outcome.copied is True
    assert kinds(bot) == ["copy"]
    assert bot.sent[0][1:] == (9, 77, 5, True)      # кнопка уходит вместе с копией
    assert len(outcome.message_ids) == 1            # номер попал в журнал — рассылку можно отозвать
    assert content.get_caption_limit() == 1024


async def test_once_the_limit_is_known_the_copy_is_sent_straight_away():
    content.set_caption_limit(1024)
    bot = Bot(hard_limit=1024)
    outcome = await send_block(block(), bot, 9)
    assert kinds(bot) == ["copy"] and outcome.copied is True


async def test_if_telegram_refuses_the_copy_too_text_goes_second_and_the_reason_is_reported():
    bot = Bot(hard_limit=1024, copy_limit=1024)
    outcome = await send_block(block(), bot, 9)
    assert outcome.ok and outcome.caption_split is True and outcome.copy_failed is True
    assert kinds(bot) == ["photo", "text"]
    assert outcome.caption_len == 1500
    assert content.get_copy_status() is False
    # дальше копию не пробуем: лишних неудачных запросов на каждого получателя не будет
    bot2 = Bot(hard_limit=1024, copy_limit=1024)
    await send_block(block(), bot2, 9)
    assert kinds(bot2) == ["photo", "text"]


async def test_other_copy_errors_fall_back_to_split_without_disabling_copy():
    content.set_caption_limit(1024)
    bot = Bot(copy_error="Bad Request: message to copy not found")
    outcome = await send_block(block(), bot, 9)
    assert outcome.ok and outcome.caption_split is True and kinds(bot) == ["photo", "text"]
    assert content.get_copy_status() is None


async def test_text_with_placeholders_cannot_be_copied():
    content.set_caption_limit(1024)
    bot = Bot()
    outcome = await send_block(block(text="Привет, {name}! " + LONG), bot, 9)
    assert kinds(bot) == ["photo", "text"] and outcome.caption_split is True and outcome.copy_failed is False


async def test_block_without_a_source_is_split_as_before():
    content.set_caption_limit(1024)
    bot = Bot()
    outcome = await send_block(ContentBlock(LONG, "photo", "P"), bot, 9)
    assert kinds(bot) == ["photo", "text"] and outcome.caption_split is True


async def test_short_caption_is_not_copied():
    bot = Bot()
    outcome = await send_block(block(text="Коротко"), bot, 9)
    assert kinds(bot) == ["photo"] and outcome.copied is False


async def test_long_album_caption_is_copied_as_an_album():
    content.set_caption_limit(1024)
    album = [{"kind": "photo", "file_id": "A"}, {"kind": "photo", "file_id": "B"}]
    bot = Bot()
    item = ContentBlock(LONG, "album", None, [], album=album, source_chat=77, source_ids=[5, 6])
    outcome = await send_block(item, bot, 9)
    assert bot.sent == [("copies", 9, 77, [5, 6])]
    assert outcome.copied is True and len(outcome.message_ids) == 2


def test_notes_name_the_length_and_the_reason():
    class Out:
        caption_split = True
        caption_len = 1500
        copy_failed = False
        copied = False
        custom_emoji_lost = False

    content.set_caption_limit(1024)
    note = preview_notes([Out()])
    assert "1500" in note and "1024" in note and "Premium" in note

    Out.caption_len = 2600
    note = preview_notes([Out()])
    assert "2600" in note and "2048" in note

    Out.caption_len, Out.copy_failed = 1500, True
    assert "копи" in preview_notes([Out()]).lower()


def test_notes_confirm_a_successful_copy():
    class Out:
        caption_split = False
        caption_len = 1500
        copy_failed = False
        copied = True
        custom_emoji_lost = False

    note = preview_notes([Out()])
    assert "1500" in note and "одним сообщением" in note


# ---------------------------------------------------------------- сбор рассылки и движок


def photo_with_caption(message_id: int, caption: str):
    from aiogram.types import PhotoSize, User

    return Message(
        message_id=message_id, date=datetime.now(timezone.utc), chat=Chat(id=ADMIN_ID, type="private"),
        from_user=User(id=ADMIN_ID, is_bot=False, first_name="Админ"),
        photo=[PhotoSize(file_id="P1", file_unique_id="u1", width=10, height=10)], caption=caption,
    )


async def _draft(dp, bot):
    data = await dp.fsm.get_context(bot, chat_id=ADMIN_ID, user_id=ADMIN_ID).get_data()
    return data.get("messages", [])


async def _start(dp, bot):
    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:bc:new", user_id=ADMIN_ID))


async def test_draft_remembers_where_the_original_message_lives(stack):
    dp, bot, session, deps = stack
    await _start(dp, bot)
    await feed(dp, bot, message=photo_with_caption(31, LONG))
    await feed(dp, bot, message=album_message(40, "A1", caption="Альбом"))
    await feed(dp, bot, message=album_message(41, "A2"))
    single, album = await _draft(dp, bot)
    assert (single["src_chat"], single["src_id"]) == (ADMIN_ID, 31)
    assert album["src_chat"] == ADMIN_ID and [p["message_id"] for p in album["album"]] == [40, 41]


async def test_editing_a_message_drops_the_link_to_the_original(stack):
    """Правка текста/медиа меняет содержимое — копия исходного показала бы старое, поэтому ссылка снимается."""
    dp, bot, session, deps = stack
    await _start(dp, bot)
    await feed(dp, bot, message=photo_with_caption(31, LONG))
    await feed(dp, bot, callback=make_callback("a:bc:done", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:bc:dedit:0", user_id=ADMIN_ID))
    await feed(dp, bot, message=photo_with_caption(32, "Новый текст"))
    [item] = await _draft(dp, bot)
    assert item["text"] == "Новый текст" and not item.get("src_chat")


async def test_broadcast_sends_long_caption_as_copies(stack):
    dp, bot, session, deps = stack
    for uid in (1, 2):
        await deps.users.upsert(uid)
    deps.engine.sent_log = deps.sent_log
    content.set_caption_limit(1024)
    await _start(dp, bot)
    await feed(dp, bot, message=photo_with_caption(31, LONG))
    await feed(dp, bot, callback=make_callback("a:bc:done", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:bc:tosend", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:bc:seg:all", user_id=ADMIN_ID))
    session.requests.clear()

    broadcast = (await deps.broadcasts.recent(1))[0]
    stats = await deps.engine.run(broadcast["id"])

    assert stats["sent"] == 2
    copies = session.calls("CopyMessage")
    assert sorted(c.chat_id for c in copies) == [1, 2]
    assert all(c.from_chat_id == ADMIN_ID and c.message_id == 31 for c in copies)
    assert session.calls("SendPhoto") == [] and session.calls("SendMessage") == []
    rows = await deps.sent_log.since(0)
    assert len(rows) == 2 and {r["kind"] for r in rows} == {"broadcast"}


async def test_draft_preview_shows_the_copy_and_confirms_it(stack):
    dp, bot, session, deps = stack
    content.set_caption_limit(1024)
    await _start(dp, bot)
    await feed(dp, bot, message=photo_with_caption(31, LONG))
    await feed(dp, bot, callback=make_callback("a:bc:done", user_id=ADMIN_ID))
    session.requests.clear()

    await feed(dp, bot, callback=make_callback("a:bc:dprev:0", user_id=ADMIN_ID))

    assert len(session.calls("CopyMessage")) == 1
    notes = [m.text for m in session.calls("SendMessage") if "1500" in (m.text or "")]
    assert notes and "одним сообщением" in notes[0]


async def test_text_over_the_telegram_maximum_does_not_switch_copies_off():
    """Подпись длиннее 2048 не примет никто — это не значит, что копии у бота не работают."""
    content.set_caption_limit(1024)
    bot = Bot(copy_limit=1024)
    outcome = await send_block(block(text="я" * 2600), bot, 9)
    assert outcome.caption_split is True and outcome.caption_len == 2600
    assert content.get_copy_status() is None
