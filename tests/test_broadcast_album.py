"""Рассылка «как прислал»: альбом остаётся альбомом, порядок и содержимое не теряются."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from aiogram.types import Chat, Message, PhotoSize, User, Video

from bot.content import ContentBlock
from tests.test_e2e import ADMIN_ID, feed, make_callback, make_message, stack  # noqa: F401

GROUP = "album-1"


def album_message(message_id: int, file_id: str, caption: str | None = None, kind: str = "photo") -> Message:
    """Одно сообщение альбома: Telegram присылает каждый файл альбома отдельным апдейтом."""
    extra: dict = {}
    if kind == "photo":
        extra["photo"] = [PhotoSize(file_id=file_id, file_unique_id=f"u-{file_id}", width=10, height=10)]
    else:
        extra["video"] = Video(
            file_id=file_id, file_unique_id=f"u-{file_id}", width=10, height=10, duration=1
        )
    return Message(
        message_id=message_id,
        date=datetime.now(timezone.utc),
        chat=Chat(id=ADMIN_ID, type="private"),
        from_user=User(id=ADMIN_ID, is_bot=False, first_name="Админ"),
        media_group_id=GROUP,
        caption=caption,
        **extra,
    )


async def _draft(dp, bot) -> list[dict]:
    data = await dp.fsm.get_context(bot, chat_id=ADMIN_ID, user_id=ADMIN_ID).get_data()
    return data.get("messages", [])


async def _start_collecting(dp, bot) -> None:
    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:bc:new", user_id=ADMIN_ID))


async def test_album_is_collected_as_one_message_in_original_order(stack):
    """Баг: каждое фото альбома становилось отдельным сообщением рассылки."""
    dp, bot, session, deps = stack
    await _start_collecting(dp, bot)

    # Telegram может доставить части альбома не по порядку
    for message in (
        album_message(52, "P3"),
        album_message(50, "P1", caption="Подпись к альбому"),
        album_message(51, "P2"),
    ):
        await feed(dp, bot, message=message)

    items = await _draft(dp, bot)
    assert len(items) == 1
    assert items[0]["media_kind"] == "album"
    assert [m["file_id"] for m in items[0]["album"]] == ["P1", "P2", "P3"]
    assert items[0]["text"] == "Подпись к альбому"


async def test_album_parts_arriving_in_parallel_are_not_lost(stack):
    """Части альбома приходят параллельными апдейтами — сбор не должен терять их."""
    dp, bot, session, deps = stack
    await _start_collecting(dp, bot)

    await asyncio.gather(
        *(
            feed(dp, bot, message=album_message(60 + i, f"P{i}", caption="Альбом" if i == 0 else None))
            for i in range(6)
        )
    )

    items = await _draft(dp, bot)
    assert len(items) == 1
    assert [m["file_id"] for m in items[0]["album"]] == [f"P{i}" for i in range(6)]


async def test_text_album_text_keep_their_order(stack):
    dp, bot, session, deps = stack
    await _start_collecting(dp, bot)

    await feed(dp, bot, message=make_message("Вступление", user_id=ADMIN_ID, message_id=40))
    await feed(dp, bot, message=album_message(41, "P1"))
    await feed(dp, bot, message=album_message(42, "P2"))
    await feed(dp, bot, message=make_message("Концовка", user_id=ADMIN_ID, message_id=43))

    items = await _draft(dp, bot)
    assert [m["media_kind"] for m in items] == [None, "album", None]
    assert [m["text"] for m in items] == ["Вступление", None, "Концовка"]


async def test_late_part_of_earlier_message_goes_before_later_one(stack):
    """Параллельная обработка: сообщение 70 дошло до обработчика позже, чем 71 — порядок всё равно 70, 71."""
    dp, bot, session, deps = stack
    await _start_collecting(dp, bot)

    await feed(dp, bot, message=make_message("Второе", user_id=ADMIN_ID, message_id=71))
    await feed(dp, bot, message=make_message("Первое", user_id=ADMIN_ID, message_id=70))

    assert [m["text"] for m in await _draft(dp, bot)] == ["Первое", "Второе"]


async def test_album_goes_to_people_as_one_media_group(stack):
    dp, bot, session, deps = stack
    for uid in (1, 2):
        await deps.users.upsert(uid)
    deps.engine.sent_log = deps.sent_log
    await _start_collecting(dp, bot)
    await feed(dp, bot, message=album_message(50, "P1", caption="Подпись"))
    await feed(dp, bot, message=album_message(51, "V2", kind="video"))
    await feed(dp, bot, callback=make_callback("a:bc:done", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:bc:tosend", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:bc:seg:all", user_id=ADMIN_ID))
    session.requests.clear()

    broadcast = (await deps.broadcasts.recent(1))[0]
    stats = await deps.engine.run(broadcast["id"])

    assert stats["sent"] == 2
    groups = session.calls("SendMediaGroup")
    assert sorted(g.chat_id for g in groups) == [1, 2]
    assert session.calls("SendPhoto") == [] and session.calls("SendVideo") == []
    media = groups[0].media
    assert [type(m).__name__ for m in media] == ["InputMediaPhoto", "InputMediaVideo"]
    assert [m.media for m in media] == ["P1", "V2"]
    assert media[0].caption == "Подпись" and media[1].caption is None
    # номера всех сообщений альбома попали в журнал — иначе рассылку нельзя было бы удалить у людей
    rows = await deps.sent_log.since(0)
    assert len(rows) == 4 and {r["kind"] for r in rows} == {"broadcast"}


async def test_album_with_too_long_caption_sends_text_separately():
    class Bot:
        def __init__(self):
            self.calls = []

        async def send_media_group(self, chat_id, media):
            self.calls.append(("group", chat_id, media))
            return []

        async def send_message(self, chat_id, text, reply_markup=None):
            self.calls.append(("text", chat_id, text))
            return None

    block = ContentBlock(
        text="Я" * 3000,
        media_kind="album",
        album=[{"kind": "photo", "file_id": "P1"}, {"kind": "photo", "file_id": "P2"}],
    )
    bot = Bot()
    for factory in block.factories(bot, 5):
        await factory()

    assert [c[0] for c in bot.calls] == ["group", "text"]
    assert all(m.caption is None for m in bot.calls[0][2])
    assert bot.calls[1][2] == "Я" * 3000


async def test_album_preview_in_draft_sends_the_whole_album(stack):
    dp, bot, session, deps = stack
    await _start_collecting(dp, bot)
    await feed(dp, bot, message=album_message(50, "P1", caption="Подпись"))
    await feed(dp, bot, message=album_message(51, "P2"))
    await feed(dp, bot, callback=make_callback("a:bc:done", user_id=ADMIN_ID))
    session.requests.clear()

    await feed(dp, bot, callback=make_callback("a:bc:dprev:0", user_id=ADMIN_ID))

    groups = session.calls("SendMediaGroup")
    assert len(groups) == 1 and [m.media for m in groups[0].media] == ["P1", "P2"]


async def test_album_cannot_get_buttons_telegram_does_not_allow_them(stack):
    dp, bot, session, deps = stack
    await _start_collecting(dp, bot)
    await feed(dp, bot, message=album_message(50, "P1"))
    await feed(dp, bot, message=album_message(51, "P2"))
    await feed(dp, bot, callback=make_callback("a:bc:done", user_id=ADMIN_ID))
    session.requests.clear()

    await feed(dp, bot, callback=make_callback("a:bc:dbtn:0", user_id=ADMIN_ID))

    state = await dp.fsm.get_context(bot, chat_id=ADMIN_ID, user_id=ADMIN_ID).get_state()
    assert state is None or "waiting_buttons" not in state
    alerts = [c for c in session.calls("AnswerCallbackQuery") if c.show_alert]
    assert alerts and "альбом" in alerts[0].text.lower()


async def test_old_broadcasts_without_album_key_still_send_as_before(stack):
    dp, bot, session, deps = stack
    await deps.users.upsert(1)
    bid, _ = await deps.engine.prepare(
        ADMIN_ID, "all", [{"text": "Старый формат", "media_kind": None, "file_id": None, "buttons": []}]
    )
    session.requests.clear()

    await deps.engine.run(bid)

    assert [c.text for c in session.calls("SendMessage")] == ["Старый формат"]
