"""Сквозные проверки: апдейты идут через настоящий диспетчер с подменённой сессией."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatInviteLink,
    ChatMemberLeft,
    ChatMemberMember,
    Message,
    MessageEntity,
    MessageId,
    Update,
    User,
)

from bot.config import Config
from bot.deps import Deps
from tests.conftest import fresh_dispatcher

FAKE_TOKEN = "123456789:AAFakeTokenForTestsOnly_0123456789ab"
ADMIN_ID = 99
USER_ID = 1

BOT_USER = User(id=777, is_bot=True, first_name="FunnelBot", username="funnel_bot")


class MockSession(BaseSession):
    """Ловит вызовы Telegram API и отвечает правдоподобными объектами."""

    def __init__(self, subscribed: bool = True) -> None:
        super().__init__()
        self.requests: list = []
        self.subscribed = subscribed
        self.keep_custom_emoji = False  # True — имитируем бота, которому Telegram разрешил кастомные эмодзи

    def names(self) -> list[str]:
        return [type(r).__name__ for r in self.requests]

    def calls(self, name: str) -> list:
        return [r for r in self.requests if type(r).__name__ == name]

    async def close(self) -> None:
        return None

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        name = type(method).__name__
        if name == "GetMe":
            return BOT_USER
        if name == "GetChatMember":
            user = User(id=USER_ID, is_bot=False, first_name="Вася")
            return ChatMemberMember(user=user, status="member") if self.subscribed else ChatMemberLeft(
                user=user, status="left"
            )
        if name == "CreateChatInviteLink":
            return ChatInviteLink(
                invite_link="https://t.me/+personal",
                creator=BOT_USER,
                creates_join_request=False,
                is_primary=False,
                is_revoked=False,
            )
        if name == "CopyMessage":
            return MessageId(message_id=1)
        if name == "GetChat":
            chat_id = method.chat_id
            resolved_id = chat_id if isinstance(chat_id, int) else 777
            return Chat(
                id=resolved_id,
                type="private",
                username=chat_id.lstrip("@") if isinstance(chat_id, str) else None,
            )
        if name.startswith("Send"):
            sent_text = getattr(method, "text", None) or getattr(method, "caption", None)
            entities = None
            if self.keep_custom_emoji and sent_text and "<tg-emoji" in sent_text:
                entities = [MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id="1")]
            return Message(
                message_id=len(self.requests) + 100,
                date=datetime.now(timezone.utc),
                chat=Chat(id=getattr(method, "chat_id", USER_ID), type="private"),
                from_user=BOT_USER,
                text=sent_text if name == "SendMessage" else None,
                entities=entities if name == "SendMessage" else None,
            )
        return True


def make_message(text: str, user_id: int = USER_ID, message_id: int = 10) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Вася", username="vasya"),
        text=text,
    )


def make_callback(data: str, user_id: int = USER_ID) -> CallbackQuery:
    return CallbackQuery(
        id="cb1",
        from_user=User(id=user_id, is_bot=False, first_name="Вася", username="vasya"),
        chat_instance="inst",
        data=data,
        message=Message(
            message_id=11,
            date=datetime.now(timezone.utc),
            chat=Chat(id=user_id, type="private"),
            from_user=BOT_USER,
            text="меню",
        ),
    )


@pytest.fixture
async def stack(db):
    session = MockSession()
    bot = Bot(
        FAKE_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    config = Config(
        bot_token=FAKE_TOKEN,
        admin_ids=(ADMIN_ID,),
        db_path=":memory:",
        messages_per_second=0,
        tick_seconds=60,
    )
    deps = Deps.build(config, db, bot)
    from bot.broadcast import BroadcastEngine

    deps.engine = BroadcastEngine(bot, deps.users, deps.broadcasts, deps.limiter)
    dp = fresh_dispatcher(deps)
    yield dp, bot, session, deps
    await bot.session.close()


async def feed(dp, bot, *, message=None, callback=None):
    update = Update(update_id=1, message=message, callback_query=callback)
    await dp.feed_update(bot, update)


async def test_start_sends_note_and_menu(stack):
    dp, bot, session, deps = stack
    media_id = await deps.media.save("krug", "video_note", "FILE_NOTE")
    await deps.settings.set("welcome_note_media_id", str(media_id))
    await deps.settings.set("channel_url", "https://t.me/mychannel")

    await feed(dp, bot, message=make_message("/start ig_reels"))

    assert session.names() == ["SendVideoNote", "SendMessage"]
    menu = session.calls("SendMessage")[0]
    buttons = menu.reply_markup.inline_keyboard
    assert buttons[0][0].url == "https://t.me/mychannel"
    assert buttons[1][0].callback_data == "check_sub"
    assert (await deps.users.get(USER_ID))["source"] == "ig_reels"


async def test_check_subscription_delivers_material_and_starts_funnel(stack):
    dp, bot, session, deps = stack
    doc_id = await deps.media.save("gaid", "document", "FILE_DOC")
    await deps.material.add_block(text="Вот гайд", media_id=doc_id)
    await deps.settings.set("private_channel_id", "-1009999999999")
    await deps.funnel.add_step(3600, text="Прогрев 1")
    await deps.settings.set("channel_id", "-1001111111111")
    await feed(dp, bot, message=make_message("/start"))
    session.requests.clear()

    await feed(dp, bot, callback=make_callback("check_sub"))

    names = session.names()
    assert "GetChatMember" in names          # подписку реально спросили у Telegram
    assert "SendDocument" in names           # материал ушёл
    assert "CreateChatInviteLink" in names   # персональная ссылка создана
    assert "AnswerCallbackQuery" in names
    assert (await deps.users.get(USER_ID))["material_sent_at"] is not None
    assert await deps.funnel.pending_count(USER_ID) == 1


async def test_check_without_subscription_gives_nothing(stack):
    dp, bot, session, deps = stack
    session.subscribed = False
    await deps.settings.set("channel_id", "-1001111111111")
    await deps.material.add_block(text="Материал")
    await feed(dp, bot, message=make_message("/start"))
    session.requests.clear()

    await feed(dp, bot, callback=make_callback("check_sub"))

    assert "SendMessage" not in session.names()
    answer = session.calls("AnswerCallbackQuery")[0]
    assert answer.show_alert is True
    assert (await deps.users.get(USER_ID))["material_sent_at"] is None


async def test_admin_menu_opens_only_for_admin(stack):
    dp, bot, session, deps = stack

    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    assert "Админка" in session.calls("SendMessage")[0].text

    session.requests.clear()
    await feed(dp, bot, message=make_message("/admin", user_id=USER_ID))
    texts = [m.text for m in session.calls("SendMessage")]
    assert not any("Админка" in t for t in texts)


async def test_admin_uploads_media_through_dialog(stack):
    dp, bot, session, deps = stack
    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:media:add", user_id=ADMIN_ID))

    # админ присылает кружок
    note_message = Message(
        message_id=20,
        date=datetime.now(timezone.utc),
        chat=Chat(id=ADMIN_ID, type="private"),
        from_user=User(id=ADMIN_ID, is_bot=False, first_name="Босс"),
        video_note={"file_id": "NOTE_FILE", "file_unique_id": "u1", "length": 240, "duration": 5},
    )
    await feed(dp, bot, message=note_message)
    await feed(dp, bot, message=make_message("krug privet", user_id=ADMIN_ID, message_id=21))

    saved = await deps.media.get_by_slug("krug_privet")
    assert saved is not None and saved["file_id"] == "NOTE_FILE" and saved["kind"] == "video_note"


async def test_admin_builds_funnel_step(stack):
    """Шаг из нескольких сообщений: задержка → сообщения подряд (текст, фото) → «Готово»."""
    from bot.repo.funnel import step_messages

    dp, bot, session, deps = stack
    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:fun:add", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("2ч", user_id=ADMIN_ID, message_id=30))
    await feed(dp, bot, message=make_message("Текст прогрева", user_id=ADMIN_ID, message_id=31))
    await feed(dp, bot, message=make_photo_message("PHOTO1", 32))
    await feed(dp, bot, message=make_message("Третье сообщение", user_id=ADMIN_ID, message_id=33))
    await feed(dp, bot, callback=make_callback("a:fun:adone", user_id=ADMIN_ID))

    steps = await deps.funnel.list_steps()
    assert len(steps) == 1
    assert steps[0]["delay_seconds"] == 7200
    assert steps[0]["text"] == "Текст прогрева"
    msgs = step_messages(steps[0])
    assert [m["media_kind"] for m in msgs] == [None, "photo", None]
    assert msgs[1]["file_id"] == "PHOTO1" and msgs[2]["text"] == "Третье сообщение"
    assert await dp.fsm.get_context(bot, chat_id=ADMIN_ID, user_id=ADMIN_ID).get_state() is None


async def test_admin_cannot_finish_step_without_messages(stack):
    dp, bot, session, deps = stack
    await feed(dp, bot, callback=make_callback("a:fun:add", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("1ч", user_id=ADMIN_ID, message_id=30))
    await feed(dp, bot, callback=make_callback("a:fun:adone", user_id=ADMIN_ID))
    assert await deps.funnel.list_steps() == []


async def _three_message_step(deps):
    return await deps.funnel.add_step(
        3600, text="Первое", backfill=False,
        extra_messages=[
            {"text": "Второе", "media_kind": None, "file_id": None, "buttons": []},
            {"text": "Третье", "media_kind": None, "file_id": None, "buttons": []},
        ],
    )


async def _messages(deps, step_id):
    from bot.repo.funnel import step_messages

    row = next(s for s in await deps.funnel.list_steps() if s["id"] == step_id)
    return step_messages(row)


async def test_admin_step_buttons_are_per_message(stack):
    dp, bot, session, deps = stack
    step_id = await _three_message_step(deps)
    await feed(dp, bot, callback=make_callback(f"a:fun:mb:{step_id}:1", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("Канал | https://t.me/x", user_id=ADMIN_ID, message_id=70))
    msgs = await _messages(deps, step_id)
    assert [len(m["buttons"]) for m in msgs] == [0, 1, 0]
    assert msgs[1]["buttons"][0] == {"text": "Канал", "url": "https://t.me/x"}
    # кнопки первого сообщения живут в прежней колонке шага
    await feed(dp, bot, callback=make_callback(f"a:fun:mb:{step_id}:0", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("Урок | https://t.me/y", user_id=ADMIN_ID, message_id=71))
    assert "Урок" in (await deps.funnel.get_step(step_id))["buttons_json"]
    await feed(dp, bot, callback=make_callback(f"a:fun:mb:{step_id}:1", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("-", user_id=ADMIN_ID, message_id=72))
    assert [len(m["buttons"]) for m in await _messages(deps, step_id)] == [1, 0, 0]


async def test_admin_edits_one_message_of_step_keeping_its_buttons(stack):
    dp, bot, session, deps = stack
    step_id = await _three_message_step(deps)
    await feed(dp, bot, callback=make_callback(f"a:fun:mb:{step_id}:2", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("Кн | https://t.me/z", user_id=ADMIN_ID, message_id=73))
    await feed(dp, bot, callback=make_callback(f"a:fun:me:{step_id}:2", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_photo_message("NEWPH", 74))
    msgs = await _messages(deps, step_id)
    assert [m["text"] for m in msgs[:2]] == ["Первое", "Второе"]
    assert msgs[2]["media_kind"] == "photo" and msgs[2]["file_id"] == "NEWPH"
    assert msgs[2]["buttons"][0]["text"] == "Кн"          # правка содержимого кнопки не трогает


async def test_admin_adds_messages_to_existing_step(stack):
    dp, bot, session, deps = stack
    step_id = await deps.funnel.add_step(3600, text="Единственное", backfill=False)
    await feed(dp, bot, callback=make_callback(f"a:fun:madd:{step_id}", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("Добавленное 1", user_id=ADMIN_ID, message_id=75))
    await feed(dp, bot, message=make_photo_message("PH2", 76))
    await feed(dp, bot, callback=make_callback(f"a:fun:msgs:{step_id}", user_id=ADMIN_ID))
    msgs = await _messages(deps, step_id)
    assert [m["text"] for m in msgs] == ["Единственное", "Добавленное 1", None]
    assert msgs[2]["file_id"] == "PH2"
    assert await dp.fsm.get_context(bot, chat_id=ADMIN_ID, user_id=ADMIN_ID).get_state() is None


async def test_admin_reorders_and_deletes_step_messages(stack):
    dp, bot, session, deps = stack
    step_id = await _three_message_step(deps)
    await feed(dp, bot, callback=make_callback(f"a:fun:mu:{step_id}:2", user_id=ADMIN_ID))
    assert [m["text"] for m in await _messages(deps, step_id)] == ["Первое", "Третье", "Второе"]
    await feed(dp, bot, callback=make_callback(f"a:fun:mu:{step_id}:1", user_id=ADMIN_ID))   # на первое место
    assert [m["text"] for m in await _messages(deps, step_id)] == ["Третье", "Первое", "Второе"]
    assert (await deps.funnel.get_step(step_id))["text"] == "Третье"          # колонка шага следует за порядком
    await feed(dp, bot, callback=make_callback(f"a:fun:md:{step_id}:0", user_id=ADMIN_ID))
    assert [m["text"] for m in await _messages(deps, step_id)] == ["Первое", "Третье", "Второе"]
    await feed(dp, bot, callback=make_callback(f"a:fun:mx:{step_id}:0", user_id=ADMIN_ID))
    assert [m["text"] for m in await _messages(deps, step_id)] == ["Третье", "Второе"]
    await feed(dp, bot, callback=make_callback(f"a:fun:mx:{step_id}:0", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback(f"a:fun:mx:{step_id}:0", user_id=ADMIN_ID))   # последнее не удаляется
    assert [m["text"] for m in await _messages(deps, step_id)] == ["Второе"]


async def test_admin_step_preview_sends_every_message(stack):
    dp, bot, session, deps = stack
    step_id = await _three_message_step(deps)
    session.requests.clear()
    await feed(dp, bot, callback=make_callback(f"a:fun:prev:{step_id}", user_id=ADMIN_ID))
    assert [m.text for m in session.calls("SendMessage")] == ["Первое", "Второе", "Третье"]
    session.requests.clear()
    await feed(dp, bot, callback=make_callback(f"a:fun:mp:{step_id}:1", user_id=ADMIN_ID))
    assert [m.text for m in session.calls("SendMessage")] == ["Второе"]


async def test_album_of_photos_keeps_every_file_in_order(stack):
    """Несколько файлов подряд (альбом) приходят параллельно и не должны ни затереть, ни подменить друг друга."""
    import asyncio

    dp, bot, session, deps = stack
    await feed(dp, bot, callback=make_callback("a:fun:add", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("1ч", user_id=ADMIN_ID, message_id=30))
    await asyncio.gather(*(feed(dp, bot, message=make_photo_message(f"P{i}", 40 + i)) for i in range(4)))
    await feed(dp, bot, callback=make_callback("a:fun:adone", user_id=ADMIN_ID))
    msgs = await _messages(deps, (await deps.funnel.list_steps())[0]["id"])
    assert sorted(m["file_id"] for m in msgs) == ["P0", "P1", "P2", "P3"]

    step_id = (await deps.funnel.list_steps())[0]["id"]
    await feed(dp, bot, callback=make_callback(f"a:fun:madd:{step_id}", user_id=ADMIN_ID))
    await asyncio.gather(*(feed(dp, bot, message=make_photo_message(f"Q{i}", 60 + i)) for i in range(3)))
    assert len(await _messages(deps, step_id)) == 7


async def test_commands_are_not_collected_as_step_messages(stack):
    dp, bot, session, deps = stack
    await feed(dp, bot, callback=make_callback("a:fun:add", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("1ч", user_id=ADMIN_ID, message_id=30))
    await feed(dp, bot, message=make_message("/help", user_id=ADMIN_ID, message_id=31))
    await feed(dp, bot, message=make_message("Настоящее", user_id=ADMIN_ID, message_id=32))
    await feed(dp, bot, callback=make_callback("a:fun:adone", user_id=ADMIN_ID))
    assert [m["text"] for m in await _messages(deps, (await deps.funnel.list_steps())[0]["id"])] == ["Настоящее"]


async def test_double_done_creates_only_one_step(stack):
    import asyncio

    dp, bot, session, deps = stack
    await feed(dp, bot, callback=make_callback("a:fun:add", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("1ч", user_id=ADMIN_ID, message_id=30))
    await feed(dp, bot, message=make_message("Текст", user_id=ADMIN_ID, message_id=31))
    await asyncio.gather(*(feed(dp, bot, callback=make_callback("a:fun:adone", user_id=ADMIN_ID)) for _ in range(2)))
    assert len(await deps.funnel.list_steps()) == 1


async def test_step_card_and_list_show_message_count(stack):
    dp, bot, session, deps = stack
    step_id = await _three_message_step(deps)
    session.requests.clear()
    await feed(dp, bot, callback=make_callback(f"a:fun:s:{step_id}", user_id=ADMIN_ID))
    edit = session.calls("EditMessageText")[-1]
    labels = [b.text for row in edit.reply_markup.inline_keyboard for b in row]
    assert "📨 Сообщения (3)" in labels
    assert "Второе" in edit.text and "Третье" in edit.text
    await feed(dp, bot, callback=make_callback("a:fun", user_id=ADMIN_ID))
    assert "📨3" in session.calls("EditMessageText")[-1].text


async def test_admin_edits_funnel_step_content_and_buttons(stack):
    """Правка уже созданного шага прогрева — текст/медиа и кнопки отдельно."""
    dp, bot, session, deps = stack
    step_id = await deps.funnel.add_step(
        delay_seconds=3600, text="Старый текст", buttons=[{"text": "Старая", "url": "https://old.test"}]
    )

    await feed(dp, bot, callback=make_callback(f"a:fun:ed:{step_id}", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("Новый текст шага", user_id=ADMIN_ID, message_id=50))

    step = await deps.funnel.get_step(step_id)
    assert step["text"] == "Новый текст шага"
    assert "Старая" in step["buttons_json"]  # кнопки правкой контента не тронуты

    await feed(dp, bot, callback=make_callback(f"a:fun:btn:{step_id}", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("Новая | https://new.test", user_id=ADMIN_ID, message_id=51))

    step = await deps.funnel.get_step(step_id)
    assert step["text"] == "Новый текст шага"  # текст правкой кнопок не тронут
    assert "Новая" in step["buttons_json"] and "Старая" not in step["buttons_json"]

    await feed(dp, bot, callback=make_callback(f"a:fun:btn:{step_id}", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("-", user_id=ADMIN_ID, message_id=52))
    step = await deps.funnel.get_step(step_id)
    assert step["buttons_json"] == "[]"


async def test_admin_broadcast_end_to_end(stack):
    dp, bot, session, deps = stack
    for uid in (1, 2, 3):
        await deps.users.upsert(uid)

    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:bc:new", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("Привет всем!", user_id=ADMIN_ID, message_id=40))
    await feed(dp, bot, callback=make_callback("a:bc:done", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:bc:tosend", user_id=ADMIN_ID))
    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:bc:seg:all", user_id=ADMIN_ID))

    broadcast = (await deps.broadcasts.recent(1))[0]
    targets = await deps.broadcasts.pending_targets(broadcast["id"])
    assert sorted(targets) == [1, 2, 3]

    stats = await deps.engine.run(broadcast["id"])
    assert stats["sent"] == 3
    sent = session.calls("SendMessage")
    assert sorted(c.chat_id for c in sent) == [1, 2, 3]
    assert all(c.text == "Привет всем!" for c in sent)


async def test_broadcast_draft_add_buttons_edit_and_reorder(stack):
    """Черновик рассылки: список сообщений, кнопки, правка текста, удаление и порядок."""
    dp, bot, session, deps = stack
    await deps.users.upsert(1)

    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:bc:new", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("Первое сообщение", user_id=ADMIN_ID, message_id=60))
    await feed(dp, bot, message=make_message("Второе сообщение", user_id=ADMIN_ID, message_id=61))
    await feed(dp, bot, callback=make_callback("a:bc:done", user_id=ADMIN_ID))

    # Открываем первое сообщение и добавляем ему кнопку
    await feed(dp, bot, callback=make_callback("a:bc:d:0", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:bc:dbtn:0", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("Канал | https://t.me/x", user_id=ADMIN_ID, message_id=62))

    data = await dp.fsm.get_context(bot, chat_id=ADMIN_ID, user_id=ADMIN_ID).get_data()
    assert data["messages"][0]["buttons"] == [{"text": "Канал", "url": "https://t.me/x"}]
    assert data["messages"][0]["text"] == "Первое сообщение"

    # Правим текст второго сообщения
    await feed(dp, bot, callback=make_callback("a:bc:dedit:1", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("Второе сообщение (правка)", user_id=ADMIN_ID, message_id=63))
    data = await dp.fsm.get_context(bot, chat_id=ADMIN_ID, user_id=ADMIN_ID).get_data()
    assert data["messages"][1]["text"] == "Второе сообщение (правка)"

    # Меняем местами и отправляем
    await feed(dp, bot, callback=make_callback("a:bc:dup:1", user_id=ADMIN_ID))
    data = await dp.fsm.get_context(bot, chat_id=ADMIN_ID, user_id=ADMIN_ID).get_data()
    assert [m["text"] for m in data["messages"]] == ["Второе сообщение (правка)", "Первое сообщение"]

    await feed(dp, bot, callback=make_callback("a:bc:tosend", user_id=ADMIN_ID))
    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:bc:seg:all", user_id=ADMIN_ID))

    broadcast = (await deps.broadcasts.recent(1))[0]
    stats = await deps.engine.run(broadcast["id"])
    assert stats["sent"] == 1

    sent = session.calls("SendMessage")
    texts = [m.text for m in sent]
    assert texts == ["Второе сообщение (правка)", "Первое сообщение"]
    # кнопка приехала со вторым (изначально первым) сообщением
    assert sent[1].reply_markup.inline_keyboard[0][0].text == "Канал"


async def test_broadcast_draft_delete_item(stack):
    dp, bot, session, deps = stack
    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:bc:new", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("Раз", user_id=ADMIN_ID, message_id=70))
    await feed(dp, bot, message=make_message("Два", user_id=ADMIN_ID, message_id=71))
    await feed(dp, bot, callback=make_callback("a:bc:done", user_id=ADMIN_ID))

    await feed(dp, bot, callback=make_callback("a:bc:ddel:0", user_id=ADMIN_ID))
    data = await dp.fsm.get_context(bot, chat_id=ADMIN_ID, user_id=ADMIN_ID).get_data()
    assert [m["text"] for m in data["messages"]] == ["Два"]


async def test_reset_is_admin_only(stack):
    dp, bot, session, deps = stack
    await deps.users.upsert(USER_ID)
    await deps.users.mark_material_sent(USER_ID)
    await feed(dp, bot, message=make_message("/reset", user_id=USER_ID))
    assert (await deps.users.get(USER_ID))["material_sent_at"] is not None

    await deps.users.upsert(ADMIN_ID)
    await deps.users.mark_material_sent(ADMIN_ID)
    await feed(dp, bot, message=make_message("/reset", user_id=ADMIN_ID))
    assert (await deps.users.get(ADMIN_ID))["material_sent_at"] is None


async def test_keyboard_stays_after_successful_check(stack):
    dp, bot, session, deps = stack
    await deps.settings.set("channel_id", "-1001111111111")
    await deps.material.add_block(text="Материал")
    await feed(dp, bot, message=make_message("/start"))
    session.requests.clear()

    await feed(dp, bot, callback=make_callback("check_sub"))

    # клавиатуру не трогаем — никаких EditMessageReplyMarkup
    assert "EditMessageReplyMarkup" not in session.names()


async def test_admin_gets_command_menu_on_start(stack):
    dp, bot, session, deps = stack
    await feed(dp, bot, message=make_message("/start", user_id=ADMIN_ID))
    scopes = [type(r).__name__ for r in session.requests]
    assert "SetMyCommands" in scopes


async def test_admin_start_sends_welcome_before_syncing_command_menu(stack):
    """Синхронизация меню команд не должна задерживать приветствие: раньше
    _sync_admin_commands дожидалась сетевого ответа Telegram ДО отправки welcome,
    удваивая задержку на каждый /start админа (лишний round-trip к Telegram API)."""
    dp, bot, session, deps = stack
    media_id = await deps.media.save("krug", "video_note", "FILE_NOTE")
    await deps.settings.set("welcome_note_media_id", str(media_id))
    await feed(dp, bot, message=make_message("/start", user_id=ADMIN_ID))
    names = session.names()
    assert names.index("SendVideoNote") < names.index("SetMyCommands")


async def test_channel_setup_reports_status_and_offers_force_save(stack, monkeypatch):
    """Если бот в канале не админ — показываем диагностику и даём сохранить вручную."""
    dp, bot, session, deps = stack
    from aiogram.types import Chat as TgChat, ChatMemberMember as TgMember

    async def fake_request(bot_, method, timeout=None):
        session.requests.append(method)
        name = type(method).__name__
        if name == "GetChat":
            return TgChat(id=-1001234567890, type="channel", title="Мой канал", username="mychan")
        if name == "GetMe":
            return BOT_USER
        if name == "GetChatMember":
            return TgMember(user=BOT_USER, status="member")   # бот не админ
        return await MockSession.make_request(session, bot_, method, timeout)

    monkeypatch.setattr(session, "make_request", fake_request)

    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:set:channel", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("@mychan", user_id=ADMIN_ID, message_id=50))

    warning = [m for m in session.calls("SendMessage") if "администратора" in (m.text or "")]
    assert warning, "должно прийти сообщение с диагностикой"
    assert "funnel_bot" in warning[0].text and "-1001234567890" in warning[0].text
    assert "member" in warning[0].text
    assert (await deps.settings.get("channel_id")) == ""  # не сохранили автоматом

    await feed(dp, bot, callback=make_callback("a:set:force", user_id=ADMIN_ID))
    assert (await deps.settings.get("channel_id")) == "-1001234567890"
    assert (await deps.settings.get("channel_url")) == "https://t.me/mychan"


async def test_channel_setup_rejects_non_channel(stack, monkeypatch):
    """Присланный чат бота или личку не принимаем за канал."""
    dp, bot, session, deps = stack
    from aiogram.types import Chat as TgChat, ChatMemberMember as TgMember

    async def fake_request(bot_, method, timeout=None):
        session.requests.append(method)
        name = type(method).__name__
        if name == "GetChat":
            return TgChat(id=BOT_USER.id, type="private", first_name="FunnelBot",
                          username="funnel_bot")
        if name == "GetMe":
            return BOT_USER
        if name == "GetChatMember":
            return TgMember(user=BOT_USER, status="member")
        return await MockSession.make_request(session, bot_, method, timeout)

    monkeypatch.setattr(session, "make_request", fake_request)

    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:set:channel", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("@funnel_bot", user_id=ADMIN_ID, message_id=60))

    warning = [m for m in session.calls("SendMessage") if "Это не канал" in (m.text or "")]
    assert warning, "должны отказать и объяснить"
    assert (await deps.settings.get("channel_id")) == ""



def make_photo_message(file_id: str, message_id: int, user_id: int = ADMIN_ID) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Босс"),
        photo=[{"file_id": file_id, "file_unique_id": f"u{message_id}", "width": 10, "height": 10}],
    )


async def test_stray_media_is_captured_and_saved(stack):
    """Файл, присланный мимо /admin, бот подхватывает сам и предлагает сохранить."""
    dp, bot, session, deps = stack

    await feed(dp, bot, message=make_photo_message("PH1", 70))
    assert len(session.calls("SendMessage")) == 1  # завели один трекер

    await feed(dp, bot, message=make_photo_message("PH2", 71))
    # второй файл редактирует тот же трекер, а не плодит новое сообщение
    assert len(session.calls("SendMessage")) == 1
    assert len(session.calls("EditMessageText")) == 1

    await feed(dp, bot, callback=make_callback("a:cap:all", user_id=ADMIN_ID))

    items = await deps.media.list(limit=10)
    assert {i["file_id"] for i in items} == {"PH1", "PH2"}
    deleted = {d.message_id for d in session.calls("DeleteMessage")}
    assert deleted == {70, 71}  # исходные сообщения убраны из чата


async def test_stray_media_cancel_deletes_without_saving(stack):
    dp, bot, session, deps = stack

    await feed(dp, bot, message=make_photo_message("PH3", 80))
    await feed(dp, bot, callback=make_callback("a:cap:cancel", user_id=ADMIN_ID))

    assert await deps.media.count() == 0
    deleted = {d.message_id for d in session.calls("DeleteMessage")}
    assert deleted == {80}


async def test_stray_media_pick_saves_only_chosen(stack):
    dp, bot, session, deps = stack

    await feed(dp, bot, message=make_photo_message("PH4", 90))
    await feed(dp, bot, message=make_photo_message("PH5", 91))
    await feed(dp, bot, callback=make_callback("a:cap:pick", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:cap:tgl:0", user_id=ADMIN_ID))  # снять первый файл
    await feed(dp, bot, callback=make_callback("a:cap:go", user_id=ADMIN_ID))

    items = await deps.media.list(limit=10)
    assert [i["file_id"] for i in items] == ["PH5"]
    # и сохранённый, и пропущенный убраны из чата — переписка не остаётся
    deleted = {d.message_id for d in session.calls("DeleteMessage")}
    assert deleted == {90, 91}



async def test_media_item_click_edits_card_not_new_message(stack):
    """Клик по файлу в медиатеке правит то же сообщение, а не шлёт превью новым."""
    dp, bot, session, deps = stack
    media_id = await deps.media.save("krug_privet", "video_note", "OLD_BOT_FILE_ID")

    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    session.requests.clear()
    await feed(dp, bot, callback=make_callback(f"a:media:s:{media_id}", user_id=ADMIN_ID))

    assert "SendMessage" not in session.names()
    assert "SendVideoNote" not in session.names()  # превью не шлём, пока не попросили
    assert "EditMessageText" in session.names()


async def test_media_preview_with_stale_file_id_shows_alert_and_stays_deletable(stack, monkeypatch):
    """Файл от старого бота (протухший file_id) не должен вешать кнопку — только алерт."""
    dp, bot, session, deps = stack
    media_id = await deps.media.save("krug_privet", "video_note", "OLD_BOT_FILE_ID")

    from aiogram.exceptions import TelegramBadRequest

    async def fake_request(bot_, method, timeout=None):
        session.requests.append(method)
        if type(method).__name__ == "SendVideoNote":
            raise TelegramBadRequest(method=method, message="wrong file identifier/HTTP URL specified")
        return await MockSession.make_request(session, bot_, method, timeout)

    monkeypatch.setattr(session, "make_request", fake_request)

    await feed(dp, bot, callback=make_callback(f"a:media:prev:{media_id}", user_id=ADMIN_ID))
    alerts = [r for r in session.calls("AnswerCallbackQuery") if r.show_alert]
    assert alerts, "должен прийти алерт вместо зависшей кнопки"

    # несмотря на битый файл, удалить его через админку по-прежнему можно
    await feed(dp, bot, callback=make_callback(f"a:media:del:{media_id}", user_id=ADMIN_ID))
    assert await deps.media.get(media_id) is None


async def test_texts_screen_always_answers_callback_even_if_rendering_fails(stack, monkeypatch):
    """Регресс: раньше, если отрисовать экран не получалось никаким способом
    (например, сохранённый текст оказался слишком длинным/с проблемными
    символами), show() падал исключением, хендлер не доходил до
    `call.answer()`, и кнопка «✏️ Тексты и кнопки» вечно крутилась в
    загрузке, ничего не происходило. Теперь show() всегда отвечает и шлёт
    понятное запасное сообщение вместо тишины."""
    dp, bot, session, deps = stack
    from aiogram.exceptions import TelegramBadRequest

    from bot.handlers.admin.common import FALLBACK_ERROR_TEXT

    async def fake_request(bot_, method, timeout=None):
        session.requests.append(method)
        name = type(method).__name__
        if name in ("EditMessageText", "EditMessageCaption"):
            raise TelegramBadRequest(method=method, message="can't parse entities: unsupported tag")
        if name == "SendMessage" and getattr(method, "text", "") != FALLBACK_ERROR_TEXT:
            raise TelegramBadRequest(method=method, message="can't parse entities: unsupported tag")
        return await MockSession.make_request(session, bot_, method, timeout)

    monkeypatch.setattr(session, "make_request", fake_request)

    await feed(dp, bot, callback=make_callback("a:set:texts", user_id=ADMIN_ID))

    assert session.calls("AnswerCallbackQuery"), "кнопка не должна зависать без ответа"
    sent_texts = [getattr(m, "text", "") for m in session.calls("SendMessage")]
    assert FALLBACK_ERROR_TEXT in sent_texts, "админ должен увидеть понятное сообщение вместо тишины"


async def test_material_block_screen_always_answers_callback_even_if_rendering_fails(stack, monkeypatch):
    """Тот же регресс, что и для «Тексты и кнопки», но для карточки блока
    материала (открывается по кнопке пункта в списке «🎁 Материал»)."""
    dp, bot, session, deps = stack
    await deps.material.add_block(text="Обычный короткий текст блока")
    block_id = (await deps.material.list_blocks())[0]["id"]

    from aiogram.exceptions import TelegramBadRequest

    from bot.handlers.admin.common import FALLBACK_ERROR_TEXT

    async def fake_request(bot_, method, timeout=None):
        session.requests.append(method)
        name = type(method).__name__
        if name in ("EditMessageText", "EditMessageCaption"):
            raise TelegramBadRequest(method=method, message="can't parse entities: unsupported tag")
        if name == "SendMessage" and getattr(method, "text", "") != FALLBACK_ERROR_TEXT:
            raise TelegramBadRequest(method=method, message="can't parse entities: unsupported tag")
        return await MockSession.make_request(session, bot_, method, timeout)

    monkeypatch.setattr(session, "make_request", fake_request)

    await feed(dp, bot, callback=make_callback(f"a:mat:s:{block_id}", user_id=ADMIN_ID))

    assert session.calls("AnswerCallbackQuery"), "кнопка не должна зависать без ответа"
    sent_texts = [getattr(m, "text", "") for m in session.calls("SendMessage")]
    assert FALLBACK_ERROR_TEXT in sent_texts, "админ должен увидеть понятное сообщение вместо тишины"


async def test_material_item_click_edits_card_not_new_message(stack):
    """Клик по блоку материала правит то же сообщение, а не шлёт обзор новым сообщением."""
    dp, bot, session, deps = stack
    await deps.material.add_block(text="Держи материал", buttons=[{"text": "Урок", "url": "https://x.test"}])
    blocks = await deps.material.list_blocks()
    block_id = blocks[0]["id"]

    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    session.requests.clear()
    await feed(dp, bot, callback=make_callback(f"a:mat:s:{block_id}", user_id=ADMIN_ID))

    assert "SendMessage" not in session.names()
    assert "EditMessageText" in session.names()

    # живое превью — по отдельной кнопке, и это уже настоящее новое сообщение
    session.requests.clear()
    await feed(dp, bot, callback=make_callback(f"a:mat:prev:{block_id}", user_id=ADMIN_ID))
    assert "SendMessage" in session.names()


async def test_admin_edits_material_block_content_and_buttons(stack):
    """Правка уже созданного блока материала — текст/медиа и кнопки отдельно."""
    dp, bot, session, deps = stack
    block_id = await deps.material.add_block(
        text="Старый материал", buttons=[{"text": "Старая", "url": "https://old.test"}]
    )

    await feed(dp, bot, callback=make_callback(f"a:mat:ed:{block_id}", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("Новый материал", user_id=ADMIN_ID, message_id=50))

    block = await deps.material.get_block(block_id)
    assert block["text"] == "Новый материал"
    assert "Старая" in block["buttons_json"]

    await feed(dp, bot, callback=make_callback(f"a:mat:btn:{block_id}", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("Новая | https://new.test", user_id=ADMIN_ID, message_id=51))

    block = await deps.material.get_block(block_id)
    assert block["text"] == "Новый материал"
    assert "Новая" in block["buttons_json"] and "Старая" not in block["buttons_json"]


async def test_admin_adds_repeat_start_block_via_dialog(stack):
    """Полный сценарий добавления блока через диалог — от кнопки до сохранения в БД."""
    dp, bot, session, deps = stack
    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:set", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:rst", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:rst:add", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("Рад видеть тебя снова 👋", user_id=ADMIN_ID, message_id=40))
    await feed(dp, bot, message=make_message("Канал | https://t.me/x", user_id=ADMIN_ID, message_id=41))

    blocks = await deps.repeat_start.list_blocks()
    assert len(blocks) == 1
    assert blocks[0]["text"] == "Рад видеть тебя снова 👋"
    assert "Канал" in blocks[0]["buttons_json"]


async def test_repeat_start_screen_lists_blocks_and_reflects_user_flow(stack):
    """То, что настроено в «Повторный /start», реально уходит пользователю на второй /start."""
    dp, bot, session, deps = stack
    await deps.repeat_start.add_block(text="С возвращением 👋")
    await deps.users.upsert(USER_ID)
    await deps.users.mark_material_sent(USER_ID)

    session.requests.clear()
    await feed(dp, bot, message=make_message("/start"))

    sent = [r for r in session.calls("SendMessage") if getattr(r, "text", "") == "С возвращением 👋"]
    assert sent, "настроенный блок должен уйти пользователю на повторный /start"


async def test_repeat_start_item_click_edits_card_not_new_message(stack):
    """Клик по блоку правит то же сообщение, а не шлёт обзор новым (как у материала)."""
    dp, bot, session, deps = stack
    await deps.repeat_start.add_block(text="Блок", buttons=[{"text": "Урок", "url": "https://x.test"}])
    block_id = (await deps.repeat_start.list_blocks())[0]["id"]

    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    session.requests.clear()
    await feed(dp, bot, callback=make_callback(f"a:rst:s:{block_id}", user_id=ADMIN_ID))

    assert "SendMessage" not in session.names()
    assert "EditMessageText" in session.names()

    session.requests.clear()
    await feed(dp, bot, callback=make_callback(f"a:rst:prev:{block_id}", user_id=ADMIN_ID))
    assert "SendMessage" in session.names()


async def test_repeat_start_toggle_and_delete(stack):
    dp, bot, session, deps = stack
    block_id = await deps.repeat_start.add_block(text="Временный блок")

    await feed(dp, bot, callback=make_callback(f"a:rst:off:{block_id}", user_id=ADMIN_ID))
    row = await deps.repeat_start.get_block(block_id)
    assert row["enabled"] == 0

    await feed(dp, bot, callback=make_callback(f"a:rst:on:{block_id}", user_id=ADMIN_ID))
    row = await deps.repeat_start.get_block(block_id)
    assert row["enabled"] == 1

    await feed(dp, bot, callback=make_callback(f"a:rst:del:{block_id}", user_id=ADMIN_ID))
    assert await deps.repeat_start.get_block(block_id) is None


async def test_stats_reset_requires_confirmation_and_wipes_users(stack):
    """«Обнулить статистику» — сначала спрашивает подтверждение, стирает только по «Да»."""
    dp, bot, session, deps = stack
    await deps.users.upsert(1, "vasya", "Вася")
    await db_has_user(deps, 1)

    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:stat", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:stat:reset", user_id=ADMIN_ID))

    # без подтверждения ничего не стёрлось
    assert await deps.users.get(1) is not None

    await feed(dp, bot, callback=make_callback("a:stat:reset:go", user_id=ADMIN_ID))
    assert await deps.users.get(1) is None


async def db_has_user(deps, tg_id):
    assert await deps.users.get(tg_id) is not None


async def test_admin_excluded_from_stats_screen(stack):
    """Админ — тестер, он не должен попадать в цифры статистики."""
    dp, bot, session, deps = stack
    await deps.users.upsert(ADMIN_ID, "boss", "Босс")
    await deps.users.upsert(1, "vasya", "Вася")

    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:stat", user_id=ADMIN_ID))

    edits = session.calls("EditMessageText")
    assert edits, "ожидали правку меню со статистикой"
    text = edits[-1].text
    assert "Всего: 1" in text


async def test_stats_file_button_sends_document(stack):
    """«Скачать таблицу» отдаёт .xlsx — собирает на лету, если планировщик ещё не успел."""
    dp, bot, session, deps = stack
    await deps.users.upsert(1, "vasya", "Вася")

    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:stat", user_id=ADMIN_ID))
    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:stat:file", user_id=ADMIN_ID))

    assert "SendDocument" in session.names()


async def test_stats_send_without_chat_shows_alert(stack):
    """Без настроенного чата «Переслать эксперту» не молчит, а объясняет, что делать."""
    dp, bot, session, deps = stack
    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:stat", user_id=ADMIN_ID))
    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:stat:send", user_id=ADMIN_ID))

    assert "SendDocument" not in session.names()
    alerts = [r for r in session.calls("AnswerCallbackQuery") if r.show_alert]
    assert alerts


async def test_stats_chat_setup_and_send(stack):
    """Задаём чат для отчётов, затем «Переслать эксперту» шлёт файл именно туда."""
    dp, bot, session, deps = stack
    await deps.users.upsert(1, "vasya", "Вася")

    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:stat", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:stat:chat", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("@expert_chat", user_id=ADMIN_ID, message_id=70))

    assert (await deps.settings.get("report_chat_id")) == "777"

    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:stat:send", user_id=ADMIN_ID))
    docs = session.calls("SendDocument")
    assert docs and docs[0].chat_id == 777


async def test_stats_chat_clear_with_dash(stack):
    """«-» сбрасывает чат для отчётов обратно на «не задан»."""
    dp, bot, session, deps = stack
    await deps.settings.set("report_chat_id", "123")

    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:stat:chat", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("-", user_id=ADMIN_ID, message_id=71))

    assert (await deps.settings.get("report_chat_id")) == ""


async def test_stray_text_from_user_is_deleted_silently(stack):
    """Постороннее сообщение обычного пользователя просто удаляется, без ответа."""
    dp, bot, session, deps = stack
    await feed(dp, bot, message=make_message("/start"))
    session.requests.clear()

    await feed(dp, bot, message=make_message("привет, а как это работает?", message_id=15))

    assert "SendMessage" not in session.names()
    deleted = {d.message_id for d in session.calls("DeleteMessage")}
    assert deleted == {15}


async def test_stray_photo_from_user_is_deleted_silently(stack):
    """То же самое для медиа — фото/видео/стикеры и т.д. тоже просто чистятся."""
    dp, bot, session, deps = stack
    await feed(dp, bot, message=make_message("/start"))
    session.requests.clear()

    await feed(dp, bot, message=make_photo_message("PH1", message_id=16, user_id=USER_ID))

    assert "SendMessage" not in session.names()
    deleted = {d.message_id for d in session.calls("DeleteMessage")}
    assert deleted == {16}


async def test_repeat_start_via_dispatcher_skips_note_second_time(stack):
    """Через реальный диспетчер: второй /start до подписки не шлёт кружок повторно."""
    dp, bot, session, deps = stack
    media_id = await deps.media.save("krug", "video_note", "FILE_NOTE")
    await deps.settings.set("welcome_note_media_id", str(media_id))

    await feed(dp, bot, message=make_message("/start"))
    assert "SendVideoNote" in session.names()

    session.requests.clear()
    await feed(dp, bot, message=make_message("/start", message_id=17))
    assert session.names() == ["SendMessage"]


async def test_admin_cycles_subscription_check_mode_of_broadcast(stack):
    """Проверка подписки в рассылке выключена, пока админ сам её не включит."""
    dp, bot, session, deps = stack
    await deps.users.upsert(1)
    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:bc:new", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("Привет!", user_id=ADMIN_ID, message_id=41))
    await feed(dp, bot, callback=make_callback("a:bc:done", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:bc:tosend", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:bc:seg:all", user_id=ADMIN_ID))
    broadcast = (await deps.broadcasts.recent(1))[0]
    bid = broadcast["id"]
    assert broadcast["sub_mode"] == "off"

    for expected in ("skip", "remind", "off"):
        await feed(dp, bot, callback=make_callback(f"a:bc:sub:{bid}", user_id=ADMIN_ID))
        assert (await deps.broadcasts.get(bid))["sub_mode"] == expected


async def test_admin_toggles_unsub_reminder_of_funnel_step(stack):
    dp, bot, session, deps = stack
    step_id = await deps.funnel.add_step(0, text="закрытый", requires_subscription=True)
    assert (await deps.funnel.get_step(step_id))["on_unsub"] == "skip"

    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback(f"a:fun:unsub:{step_id}", user_id=ADMIN_ID))
    assert (await deps.funnel.get_step(step_id))["on_unsub"] == "remind"
    await feed(dp, bot, callback=make_callback(f"a:fun:unsub:{step_id}", user_id=ADMIN_ID))
    assert (await deps.funnel.get_step(step_id))["on_unsub"] == "skip"


# --- структура админки: главное меню, «Воронка» по шагам, возвраты -----------------


def _screen(session):
    shown = [r for r in session.requests if type(r).__name__ in ("EditMessageText", "SendMessage")]
    last = shown[-1]
    return last.text, last.reply_markup


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


async def _open(dp, bot, session, data, user_id: int = ADMIN_ID):
    session.requests.clear()
    await feed(dp, bot, callback=make_callback(data, user_id=user_id))
    return _screen(session)


async def test_admin_main_menu_sections(stack):
    dp, bot, session, deps = stack
    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    _, markup = _screen(session)
    assert _callbacks(markup) == ["a:flow", "a:bc", "t:open", "a:stat", "a:set", "a:guide"]


async def test_main_menu_shows_what_is_left_to_configure(stack):
    dp, bot, session, deps = stack
    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    text, _ = _screen(session)
    assert "Осталось настроить" in text

    media_id = await deps.media.save("krug", "video_note", "F")
    await deps.settings.set("welcome_note_media_id", str(media_id))
    await deps.settings.set("channel_id", "-1001")
    await deps.material.add_block(text="Материал")
    await deps.funnel.add_step(60, text="Шаг")
    text, _ = await _open(dp, bot, session, "a:menu")
    assert "Осталось настроить" not in text
    assert "Воронка настроена" in text


async def test_flow_hub_shows_subscriber_path_in_order(stack):
    dp, bot, session, deps = stack
    text, markup = await _open(dp, bot, session, "a:flow")
    order = ["Приветствие", "Проверка подписки", "Материал", "Прогрев", "Повторный /start"]
    positions = [text.index(name) for name in order]
    assert positions == sorted(positions)
    assert _callbacks(markup) == ["a:flow:hi", "a:flow:sub", "a:mat", "a:fun", "a:rst", "a:menu"]


async def test_settings_holds_only_general_things(stack):
    dp, bot, session, deps = stack
    _, markup = await _open(dp, bot, session, "a:set")
    cbs = _callbacks(markup)
    assert cbs == ["a:media", "a:set:texts", "a:set:emoji", "a:set:report", "a:acc", "a:menu"]


async def test_flow_steps_return_to_flow_and_media_to_settings(stack):
    dp, bot, session, deps = stack
    for opener in ("a:mat", "a:fun", "a:rst", "a:flow:hi", "a:flow:sub"):
        _, markup = await _open(dp, bot, session, opener)
        assert _callbacks(markup)[-1] == "a:flow", opener
    _, markup = await _open(dp, bot, session, "a:media")
    assert _callbacks(markup)[-1] == "a:set"


async def test_input_prompts_cancel_back_to_their_own_section(stack):
    dp, bot, session, deps = stack
    cases = {
        "a:mat:add": "a:mat",
        "a:fun:add": "a:fun",
        "a:rst:add": "a:rst",
        "a:media:add": "a:media",
        "a:set:channel": "a:flow:sub",
        "a:set:private": "a:flow:sub",
        "a:flow:hi:txt": "a:flow:hi",
        "a:set:report": "a:set",
        "a:stat:chat": "a:stat",
        "a:bc:new": "a:bc",
    }
    for opener, back in cases.items():
        text, markup = await _open(dp, bot, session, opener)
        assert _callbacks(markup)[-1] == back, opener
        assert "Отмена" in markup.inline_keyboard[-1][-1].text, opener
        await feed(dp, bot, callback=make_callback("a:menu", user_id=ADMIN_ID))  # сброс состояния


async def test_material_list_is_compact_and_marks_content_type(stack):
    dp, bot, session, deps = stack
    video = await deps.media.save("v", "video", "FV")
    await deps.material.add_block(text="Первый текстовый блок с длинным описанием " * 5)
    await deps.material.add_block(media_id=video)
    text, markup = await _open(dp, bot, session, "a:mat")
    assert "📝" in text and "🎬" in text
    assert len(text) < 700
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any(label.startswith("1.") and "📝" in label for label in labels)


async def test_channel_saved_from_flow_returns_to_subscription_screen(stack):
    dp, bot, session, deps = stack
    await feed(dp, bot, callback=make_callback("a:set:channel", user_id=ADMIN_ID))
    session.requests.clear()
    await feed(dp, bot, message=make_message("-", user_id=ADMIN_ID, message_id=50))
    text, markup = _screen(session)
    assert "Проверка подписки" in text
    assert _callbacks(markup)[-1] == "a:flow"


async def test_step_card_has_two_levels(stack):
    dp, bot, session, deps = stack
    step_id = await deps.funnel.add_step(60, text="Шаг", requires_subscription=True)
    _, markup = await _open(dp, bot, session, f"a:fun:s:{step_id}")
    main = _callbacks(markup)
    assert f"a:fun:more:{step_id}" in main
    assert not any(cb.startswith(("a:fun:del:", "a:fun:gate", "a:fun:tgl", "a:fun:up")) for cb in main)

    _, markup = await _open(dp, bot, session, f"a:fun:more:{step_id}")
    more = _callbacks(markup)
    for prefix in ("a:fun:gate", "a:fun:unsub", "a:fun:tgl", "a:fun:up", "a:fun:dn", "a:fun:del:"):
        assert any(cb.startswith(prefix) for cb in more), prefix
    assert more[-1] == f"a:fun:s:{step_id}"


async def test_broadcast_draft_list_marks_types_and_stays_short(stack):
    dp, bot, session, deps = stack
    await feed(dp, bot, callback=make_callback("a:bc:new", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("Привет всем, длинный текст " * 6, user_id=ADMIN_ID, message_id=61))
    text, _ = await _open(dp, bot, session, "a:bc:done")
    assert "📝" in text
    assert len(text) < 500


# --- доступ «только статистика» ---------------------------------------------------

VIEWER_ID = 555


async def test_viewer_gets_stats_by_command_and_stranger_gets_nothing(stack):
    dp, bot, session, deps = stack
    await deps.users.upsert(1, "u1", "Вася")
    await deps.access.add(VIEWER_ID, "Клиент", "client")

    session.requests.clear()
    await feed(dp, bot, message=make_message("/stats", user_id=VIEWER_ID))
    sent = session.calls("SendMessage")
    assert len(sent) == 1 and "Статистика" in sent[0].text and sent[0].chat_id == VIEWER_ID

    session.requests.clear()
    await feed(dp, bot, message=make_message("/stats", user_id=1))  # обычный пользователь
    assert "SendMessage" not in session.names()


async def test_viewer_cannot_open_admin_panel(stack):
    dp, bot, session, deps = stack
    await deps.access.add(VIEWER_ID, "Клиент", None)
    session.requests.clear()
    await feed(dp, bot, message=make_message("/admin", user_id=VIEWER_ID))
    assert "SendMessage" not in session.names()
    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:set", user_id=VIEWER_ID))
    assert "EditMessageText" not in session.names()


async def test_admin_stats_command_works_too(stack):
    dp, bot, session, deps = stack
    session.requests.clear()
    await feed(dp, bot, message=make_message("/stats", user_id=ADMIN_ID))
    assert "Статистика" in session.calls("SendMessage")[0].text


async def test_admin_invites_viewer_by_link_and_link_works_once(stack):
    dp, bot, session, deps = stack
    text, markup = await _open(dp, bot, session, "a:acc:link:stats")
    assert "?start=v_" in text
    token = text.split("?start=v_")[1].split("<")[0].split()[0].strip()

    session.requests.clear()
    await feed(dp, bot, message=make_message(f"/start v_{token}", user_id=VIEWER_ID))
    assert await deps.access.role(VIEWER_ID) is not None
    assert await deps.users.get(VIEWER_ID) is None  # клиент не попал в воронку и статистику
    assert "/stats" in session.calls("SendMessage")[0].text

    session.requests.clear()
    await feed(dp, bot, message=make_message(f"/start v_{token}", user_id=556))
    assert await deps.access.role(556) is None
    assert await deps.users.get(556) is None


async def test_admin_adds_and_removes_viewer_by_id(stack):
    dp, bot, session, deps = stack
    await feed(dp, bot, callback=make_callback("a:acc:add:stats", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("777", user_id=ADMIN_ID, message_id=70))
    assert await deps.access.role(777) == "stats"

    text, markup = await _open(dp, bot, session, "a:acc")
    assert "777" in text
    assert "a:acc:del:777" in _callbacks(markup)
    await _open(dp, bot, session, "a:acc:del:777")
    text, _ = await _open(dp, bot, session, "a:acc:delok:777")
    assert await deps.access.role(777) is None


async def test_access_screen_cancel_returns_to_access(stack):
    dp, bot, session, deps = stack
    _, markup = await _open(dp, bot, session, "a:acc:add")
    assert _callbacks(markup)[-1] == "a:acc"


async def test_admin_role_can_open_admin_panel_stats_role_cannot(stack):
    dp, bot, session, deps = stack
    await deps.access.add(VIEWER_ID, "Клиент", None, role="admin")
    session.requests.clear()
    await feed(dp, bot, message=make_message("/admin", user_id=VIEWER_ID))
    assert "EditMessageText" in session.names() or "SendMessage" in session.names()

    await deps.access.add(600, "Просто зритель", None, role="stats")
    session.requests.clear()
    await feed(dp, bot, message=make_message("/admin", user_id=600))
    # Стороннее сообщение "/admin" от неадмина попадает под общий фолбэк-обработчик
    # (bot/handlers/user.py::fallback), который тихо удаляет любые нераспознанные
    # сообщения — это никак не даёт роли "stats" открыть админку.
    assert "SendMessage" not in session.names() and "EditMessageText" not in session.names()


async def test_owner_picks_role_when_creating_invite(stack):
    dp, bot, session, deps = stack
    text, markup = await _open(dp, bot, session, "a:acc:link")
    assert "a:acc:link:stats" in _callbacks(markup) and "a:acc:link:admin" in _callbacks(markup)

    text, _ = await _open(dp, bot, session, "a:acc:link:admin")
    token = text.split("?start=v_")[1].split("<")[0].split()[0].strip()
    await feed(dp, bot, message=make_message(f"/start v_{token}", user_id=701))
    assert await deps.access.role(701) == "admin"


# --- владельческие экраны и действия недоступны роли admin -------------------


async def test_admin_role_is_rejected_directly_by_access_router(stack):
    """Task 2 review: роль admin не должна суметь сама себя повысить через a:acc*."""
    dp, bot, session, deps = stack
    await deps.access.add(VIEWER_ID, "Клиент", None, role="admin")

    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:acc:link:admin", user_id=VIEWER_ID))
    assert "EditMessageText" not in session.names() and "SendMessage" not in session.names()
    answer = session.calls("AnswerCallbackQuery")[0]
    assert answer.show_alert is True

    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:acc", user_id=VIEWER_ID))
    assert "EditMessageText" not in session.names() and "SendMessage" not in session.names()
    assert session.calls("AnswerCallbackQuery")[0].show_alert is True


async def test_access_link_add_and_delete_rejected_for_role_admin(stack):
    """Task 3 review: guard'ы в каждом хендлере a:acc:* (не только в a:acc и a:acc:link:*),
    включая удаление реального клиента по его tg_id."""
    dp, bot, session, deps = stack
    await deps.access.add(VIEWER_ID, "Клиент", None, role="admin")
    await deps.access.add(888, "Другой клиент", None, role="stats")

    for data in ("a:acc:link", "a:acc:add", "a:acc:add:stats", "a:acc:add:admin"):
        session.requests.clear()
        await feed(dp, bot, callback=make_callback(data, user_id=VIEWER_ID))
        assert "EditMessageText" not in session.names() and "SendMessage" not in session.names(), data
        assert session.calls("AnswerCallbackQuery")[0].show_alert is True, data

    for data in ("a:acc:del:888", "a:acc:delok:888"):
        session.requests.clear()
        await feed(dp, bot, callback=make_callback(data, user_id=VIEWER_ID))
        assert "EditMessageText" not in session.names() and "SendMessage" not in session.names(), data
        assert session.calls("AnswerCallbackQuery")[0].show_alert is True, data
    assert await deps.access.role(888) == "stats"  # реальный клиент не удалился


async def test_content_admin_does_not_see_or_reach_owner_actions(stack):
    dp, bot, session, deps = stack
    await deps.access.add(VIEWER_ID, "Клиент", None, role="admin")
    await deps.users.upsert(42, "u42", "Юзер")  # чтобы было что "обнулить", если бы guard не сработал

    text, markup = await _open(dp, bot, session, "a:set", user_id=VIEWER_ID)
    assert "a:acc" not in _callbacks(markup) and "a:set:report" not in _callbacks(markup)

    text, markup = await _open(dp, bot, session, "a:flow:sub", user_id=VIEWER_ID)
    assert "a:set:channel" not in _callbacks(markup) and "a:set:private" not in _callbacks(markup)
    assert any(cb.startswith("a:set:texts:sub") for cb in _callbacks(markup))

    text, markup = await _open(dp, bot, session, "a:stat", user_id=VIEWER_ID)
    assert "a:stat:reset" not in _callbacks(markup) and "a:stat:chat" not in _callbacks(markup)

    total_before = (await deps.users.stats())["total"]
    assert total_before >= 1  # seed действительно есть, иначе проверка ниже бессмысленна

    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:stat:reset:go", user_id=VIEWER_ID))
    # Настоящий сброс удалил бы всех пользователей и отредактировал сообщение —
    # ничего из этого не должно случиться под ролью admin.
    assert (await deps.users.stats())["total"] == total_before
    assert "EditMessageText" not in session.names()
    answer = session.calls("AnswerCallbackQuery")[0]
    assert answer.show_alert is True


async def test_content_admin_cannot_reach_channel_or_report_chat_handlers_directly(stack):
    dp, bot, session, deps = stack
    await deps.access.add(VIEWER_ID, "Клиент", None, role="admin")

    for data in ("a:set:channel", "a:set:private", "a:set:force", "a:set:report", "a:stat:chat"):
        session.requests.clear()
        await feed(dp, bot, callback=make_callback(data, user_id=VIEWER_ID))
        assert "EditMessageText" not in session.names() and "SendMessage" not in session.names(), data
        assert session.calls("AnswerCallbackQuery")[0].show_alert is True, data


async def test_content_admin_cannot_force_save_pending_channel(stack):
    """a:set:force сохраняет канал в обход проверки прав Telegram. Пустой probe без
    предварительно выставленного pending_id ничего не докажет (хендлер и так молча
    отвечает "Нечего сохранять") — поэтому сперва выставляем pending_id так, как это
    сделал бы владелец через a:set:channel -> on_channel_value, когда бот не админ
    канала, и только потом дёргаем a:set:force под ролью admin."""
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey

    dp, bot, session, deps = stack
    await deps.access.add(VIEWER_ID, "Клиент", None, role="admin")

    key = StorageKey(bot_id=bot.id, chat_id=VIEWER_ID, user_id=VIEWER_ID)
    ctx = FSMContext(storage=dp.storage, key=key)
    await ctx.update_data(
        field="channel",
        pending_id="-100123456",
        pending_title="Тестовый канал",
        pending_url="https://t.me/testchan",
    )

    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:set:force", user_id=VIEWER_ID))
    assert (await deps.settings.get("channel_id")).strip() == ""
    assert (await deps.settings.get("channel_title")).strip() == ""
    assert (await deps.settings.get("channel_url")).strip() == ""
    assert "EditMessageText" not in session.names() and "SendMessage" not in session.names()
    assert session.calls("AnswerCallbackQuery")[0].show_alert is True


async def test_content_admin_cannot_open_stats_reset_confirm_screen(stack):
    """a:stat:reset — сам экран подтверждения обнуления, не только a:stat:reset:go."""
    dp, bot, session, deps = stack
    await deps.access.add(VIEWER_ID, "Клиент", None, role="admin")

    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:stat:reset", user_id=VIEWER_ID))
    assert "EditMessageText" not in session.names() and "SendMessage" not in session.names()
    assert session.calls("AnswerCallbackQuery")[0].show_alert is True


async def test_content_admin_does_not_see_delete_button_on_broadcast_card(stack):
    dp, bot, session, deps = stack
    await deps.access.add(VIEWER_ID, "Клиент", None, role="admin")

    await feed(dp, bot, callback=make_callback("a:bc:new", user_id=VIEWER_ID))
    await feed(dp, bot, message=make_message("Привет всем!", user_id=VIEWER_ID, message_id=90))
    await feed(dp, bot, callback=make_callback("a:bc:done", user_id=VIEWER_ID))
    await feed(dp, bot, callback=make_callback("a:bc:tosend", user_id=VIEWER_ID))
    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:bc:seg:all", user_id=VIEWER_ID))
    text, markup = _screen(session)
    cbs = _callbacks(markup)
    assert not any(cb.startswith("a:bc:del:") for cb in cbs)
    broadcast_id = (await deps.broadcasts.recent(5))[0]["id"]

    for data in (f"a:bc:del:{broadcast_id}", f"a:bc:delok:{broadcast_id}"):
        session.requests.clear()
        await feed(dp, bot, callback=make_callback(data, user_id=VIEWER_ID))
        assert "EditMessageText" not in session.names() and "SendMessage" not in session.names(), data
        assert session.calls("AnswerCallbackQuery")[0].show_alert is True, data
    assert await deps.broadcasts.get(broadcast_id) is not None  # не удалилась


async def test_owner_still_sees_all_owner_buttons(stack):
    """Регрессия: у владельца (ADMIN_ID) все владельческие кнопки/действия остаются на месте."""
    dp, bot, session, deps = stack
    _, markup = await _open(dp, bot, session, "a:set")
    assert "a:acc" in _callbacks(markup) and "a:set:report" in _callbacks(markup)

    _, markup = await _open(dp, bot, session, "a:flow:sub")
    assert "a:set:channel" in _callbacks(markup) and "a:set:private" in _callbacks(markup)

    _, markup = await _open(dp, bot, session, "a:stat")
    assert "a:stat:reset" in _callbacks(markup) and "a:stat:chat" in _callbacks(markup)


async def test_admin_role_revoked_access_denied_on_very_next_action(stack):
    """Review Focus: отзыв доступа (deps.access.remove) должен блокировать САМОЕ
    СЛЕДУЮЩЕЕ действие клиента — ни /admin, ни a:-колбэки не должны кэшировать роль
    где-либо между хендлерами."""
    dp, bot, session, deps = stack
    await deps.access.add(VIEWER_ID, "Клиент", None, role="admin")

    session.requests.clear()
    await feed(dp, bot, message=make_message("/admin", user_id=VIEWER_ID))
    assert "SendMessage" in session.names() or "EditMessageText" in session.names()

    await deps.access.remove(VIEWER_ID)

    session.requests.clear()
    await feed(dp, bot, message=make_message("/admin", user_id=VIEWER_ID))
    assert "SendMessage" not in session.names() and "EditMessageText" not in session.names()

    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:set", user_id=VIEWER_ID))
    assert "EditMessageText" not in session.names() and "SendMessage" not in session.names()
    assert await deps.access.role(VIEWER_ID) is None


async def test_guide_button_sends_document_to_owner(stack):
    dp, bot, session, deps = stack
    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:guide", user_id=ADMIN_ID))
    assert "SendDocument" in session.names()


async def test_guide_button_sends_document_to_admin_role_client(stack):
    dp, bot, session, deps = stack
    await deps.access.add(VIEWER_ID, "Клиент", None, role="admin")
    await feed(dp, bot, message=make_message("/admin", user_id=VIEWER_ID))
    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:guide", user_id=VIEWER_ID))
    assert "SendDocument" in session.names()


async def test_guide_button_missing_file_shows_alert_not_crash(stack, monkeypatch):
    dp, bot, session, deps = stack
    import bot.handlers.admin as admin_pkg
    monkeypatch.setattr(admin_pkg, "GUIDE_PDF_PATH", admin_pkg.GUIDE_PDF_PATH.with_name("missing.pdf"))
    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:guide", user_id=ADMIN_ID))
    assert "SendDocument" not in session.names()
    alerts = [r for r in session.calls("AnswerCallbackQuery") if r.show_alert]
    assert alerts


async def test_guide_button_rejected_for_stats_role_client(stack):
    """Review Focus: роль stats не проходит AdminFilter — прямой вызов a:guide не должен
    сработать, а не просто «наверное, сработает, раз кнопка спрятана»."""
    dp, bot, session, deps = stack
    await deps.access.add(VIEWER_ID, "Клиент", None, role="stats")
    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:guide", user_id=VIEWER_ID))
    assert "SendDocument" not in session.names()


# --- автовыдача урока и тестовый прогон -----------------------------------

LESSON_URL = "https://tkpdt.ru/lendingi150"




















async def test_admin_edits_auto_deliver_minutes(stack):
    dp, bot, session, deps = stack
    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:set:texts:sub", user_id=ADMIN_ID))
    shown = " ".join(getattr(r, "text", "") or "" for r in session.requests)
    assert "Выдать урок сам через N минут" in shown

    await feed(dp, bot, callback=make_callback("a:set:t:auto_deliver_minutes", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("30", user_id=ADMIN_ID, message_id=70))
    assert await deps.settings.get_int("auto_deliver_minutes") == 30






async def test_legacy_lc_button_still_delivers_link(stack):
    """Кнопки «клик» жили меньше суток; у уже отправленных сообщений они не должны быть мёртвыми."""
    import hashlib

    dp, bot, session, deps = stack
    url = "https://tkpdt.ru/lendingi150"
    await deps.funnel.add_step(3600, text="Пуш", buttons=[{"text": "Смотреть урок", "url": url}])
    digest = hashlib.sha1(url.encode()).hexdigest()[:16]
    await feed(dp, bot, callback=make_callback(f"lc:{digest}"))
    assert session.calls("SendMessage")[-1].reply_markup.inline_keyboard[0][0].url == url
    session.requests.clear()
    await feed(dp, bot, callback=make_callback("lc:0000000000000000"))
    assert "устарела" in " ".join(str(getattr(r, "text", "")) for r in session.requests)


# --- предпрод и отзыв публикаций ------------------------------------------


def _shown(session):
    return " ".join(getattr(r, "text", "") or "" for r in session.requests)


async def test_funnel_screen_has_preprod_and_recall_buttons(stack):
    dp, bot, session, deps = stack
    await feed(dp, bot, callback=make_callback("a:fun", user_id=ADMIN_ID))
    markup = session.calls("EditMessageText")[-1].reply_markup if session.calls("EditMessageText") else session.calls("SendMessage")[-1].reply_markup
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "a:pre" in callbacks and "a:rec" in callbacks


async def test_myid_command_replies_with_id(stack):
    dp, bot, session, deps = stack
    await feed(dp, bot, message=make_message("/myid", user_id=USER_ID))
    assert f"<code>{USER_ID}</code>" in session.calls("SendMessage")[-1].text


async def test_admin_preprod_toggle_add_clear_and_schedule(stack):
    dp, bot, session, deps = stack
    assert await deps.settings.get("preprod_mode") == "0"

    await feed(dp, bot, callback=make_callback("a:pre:tgl", user_id=ADMIN_ID))   # включить
    assert await deps.settings.get("preprod_mode") == "1"

    await feed(dp, bot, callback=make_callback("a:pre:add", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("555", user_id=ADMIN_ID, message_id=80))
    assert await deps.settings.get("preprod_user_ids") == "555"
    await feed(dp, bot, callback=make_callback("a:pre:add", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("не число", user_id=ADMIN_ID, message_id=81))
    assert await deps.settings.get("preprod_user_ids") == "555"

    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:pre:sch", user_id=ADMIN_ID))
    assert "555" in _shown(session)

    await feed(dp, bot, callback=make_callback("a:pre:clr", user_id=ADMIN_ID))
    assert await deps.settings.get("preprod_user_ids") == ""


async def test_admin_preprod_off_needs_confirmation(stack):
    dp, bot, session, deps = stack
    await deps.settings.set("preprod_mode", "1")
    await feed(dp, bot, callback=make_callback("a:pre:tgl", user_id=ADMIN_ID))     # запрос подтверждения
    assert await deps.settings.get("preprod_mode") == "1"
    await feed(dp, bot, callback=make_callback("a:pre:off", user_id=ADMIN_ID))
    assert await deps.settings.get("preprod_mode") == "0"


async def test_admin_menu_shows_preprod_banner(stack):
    dp, bot, session, deps = stack
    await deps.settings.set("preprod_mode", "1")
    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    assert "Предпрод включён" in session.calls("SendMessage")[-1].text


async def test_recall_screens_and_run(stack):
    dp, bot, session, deps = stack
    step = await deps.funnel.add_step(60, text="Пуш", backfill=False)
    await deps.users.upsert(USER_ID, "vasya", "Вася")
    import time as _t

    now = int(_t.time())
    await deps.db.execute(
        "INSERT INTO user_steps(user_id, step_id, due_at, status, sent_at) VALUES(?, ?, ?, 'sent', ?)",
        (USER_ID, step, now - 60, now - 60),
    )
    await deps.sent_log.add(USER_ID, [11, 12], "step", step, now=now - 60)

    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:rec", user_id=ADMIN_ID))
    assert "За какой период" in _shown(session)

    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:rec:w:3600", user_id=ADMIN_ID))
    text = _shown(session)
    assert "получили публикации: <b>1</b>" in text and "точно (по журналу): 2" in text

    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:rec:go:3600", user_id=ADMIN_ID))
    deleted = session.calls("DeleteMessages")
    assert deleted and sorted(deleted[0].message_ids) == [11, 12]
    row = await deps.db.fetchone("SELECT status FROM user_steps WHERE user_id = ?", (USER_ID,))
    assert row["status"] == "skipped"


async def test_recall_window_without_sends_says_nothing_to_recall(stack):
    dp, bot, session, deps = stack
    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:rec:w:900", user_id=ADMIN_ID))
    assert "ничего не отправлялось" in _shown(session)


# --- панель тестового прогона /test ---------------------------------------

TESTER_ID = 555


def _texts(session):
    return " ".join(str(getattr(r, "text", "") or "") for r in session.requests)


def _all_callbacks(session):
    out = []
    for r in session.requests:
        markup = getattr(r, "reply_markup", None)
        if markup is not None and hasattr(markup, "inline_keyboard"):
            out += [b.callback_data for row in markup.inline_keyboard for b in row]
    return out


async def test_test_panel_hidden_from_strangers(stack):
    dp, bot, session, deps = stack
    await feed(dp, bot, message=make_message("/test", user_id=USER_ID))
    assert "Тестовый прогон" not in _texts(session)
    await feed(dp, bot, callback=make_callback("t:new", user_id=USER_ID))
    assert (await deps.users.get(USER_ID)) is None or (await deps.users.get(USER_ID))["material_sent_at"] is None
    assert "SendMessage" not in session.names()


async def test_test_panel_opens_for_owner_role_admin_and_listed_tester(stack):
    dp, bot, session, deps = stack
    await deps.funnel.add_step(3600, text="Пуш")
    await deps.access.add(600, "Клиент", None, role="admin")
    await deps.settings.set("preprod_user_ids", str(TESTER_ID))
    for uid in (ADMIN_ID, 600, TESTER_ID):
        session.requests.clear()
        await feed(dp, bot, message=make_message("/test", user_id=uid))
        assert "Тестовый прогон" in _texts(session), uid
        assert {"t:new", "t:lesson", "t:next", "t:fast", "t:real", "t:ref"} <= set(_all_callbacks(session))


async def test_test_panel_button_in_admin_menu_opens_panel(stack):
    dp, bot, session, deps = stack
    await deps.funnel.add_step(3600, text="Пуш")
    await feed(dp, bot, callback=make_callback("t:open", user_id=ADMIN_ID))
    assert "Прогресс: 0 из 1" in _texts(session)


async def test_test_panel_restart_resets_and_sends_welcome(stack):
    dp, bot, session, deps = stack
    await deps.users.upsert(TESTER_ID, "t", "Тест")
    await deps.settings.set("preprod_user_ids", str(TESTER_ID))
    await deps.db.execute("UPDATE users SET material_sent_at = 5, funnel_fast = 1 WHERE tg_id = ?", (TESTER_ID,))
    await feed(dp, bot, callback=make_callback("t:new", user_id=TESTER_ID))
    user = await deps.users.get(TESTER_ID)
    assert user["material_sent_at"] is None and user["funnel_fast"] == 0
    assert "SendMessage" in session.names()


async def test_test_panel_lesson_button_starts_funnel_without_subscription(stack):
    dp, bot, session, deps = stack
    await deps.funnel.add_step(3600, text="Пуш")
    await feed(dp, bot, callback=make_callback("t:lesson", user_id=ADMIN_ID))
    user = await deps.users.get(ADMIN_ID)
    assert user["material_sent_at"] is not None
    assert await deps.funnel.pending_count(ADMIN_ID) == 1
    session.requests.clear()
    await feed(dp, bot, callback=make_callback("t:lesson", user_id=ADMIN_ID))     # повтор — не выдаёт второй раз
    assert await deps.funnel.pending_count(ADMIN_ID) == 1


async def test_test_panel_fast_and_real_buttons(stack):
    dp, bot, session, deps = stack
    await deps.funnel.add_step(3600, text="Пуш")
    await feed(dp, bot, callback=make_callback("t:lesson", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("t:fast", user_id=ADMIN_ID))
    assert (await deps.users.get(ADMIN_ID))["funnel_fast"] == 1
    assert "ускоренно" in _texts(session)
    await feed(dp, bot, callback=make_callback("t:real", user_id=ADMIN_ID))
    assert (await deps.users.get(ADMIN_ID))["funnel_fast"] == 0


async def test_test_panel_next_post_now_sends_it(stack):
    from bot.scheduler import Scheduler

    dp, bot, session, deps = stack
    deps.scheduler = Scheduler(bot=bot, users=deps.users, funnel=deps.funnel, settings=deps.settings,
                               gate=deps.gate, limiter=deps.limiter, sent_log=deps.sent_log)
    await deps.funnel.add_step(12 * 3600, text="Пост через 12 часов")
    await feed(dp, bot, callback=make_callback("t:lesson", user_id=ADMIN_ID))
    session.requests.clear()
    await feed(dp, bot, callback=make_callback("t:next", user_id=ADMIN_ID))
    assert "Пост через 12 часов" in _texts(session)
    assert "✅ 1" in _texts(session)                         # панель показывает отправленный пост


async def test_admin_role_can_manage_testers_but_not_release(stack):
    dp, bot, session, deps = stack
    await deps.access.add(600, "Клиент", None, role="admin")
    await deps.settings.set("preprod_mode", "1")
    await feed(dp, bot, callback=make_callback("a:pre:add", user_id=600))
    assert "Тестовый аккаунт" in _texts(session)
    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:pre:tgl", user_id=600))              # выпуск: только владелец
    assert (await deps.settings.get("preprod_mode")) == "1"
    await feed(dp, bot, callback=make_callback("a:pre:off", user_id=600))
    assert (await deps.settings.get("preprod_mode")) == "1"
    await feed(dp, bot, callback=make_callback("a:pre:off", user_id=ADMIN_ID))
    assert (await deps.settings.get("preprod_mode")) == "0"


async def test_test_command_works_in_the_middle_of_admin_dialog(stack):
    """Владелец пишет /test, пока бот ждёт контент шага — команда не должна уйти в диалог текстом."""
    dp, bot, session, deps = stack
    await feed(dp, bot, callback=make_callback("a:fun:add", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("1ч", user_id=ADMIN_ID, message_id=20))
    session.requests.clear()
    await feed(dp, bot, message=make_message("/test", user_id=ADMIN_ID, message_id=21))
    assert "Тестовый прогон" in _texts(session)
    assert await deps.funnel.list_steps() == []


async def test_fast_mode_survives_lesson_delivery_and_needs_no_prior_start(stack):
    dp, bot, session, deps = stack
    await deps.funnel.add_step(3600, text="Пуш")
    await feed(dp, bot, callback=make_callback("t:fast", user_id=ADMIN_ID))     # строки users ещё нет
    assert (await deps.users.get(ADMIN_ID))["funnel_fast"] == 1
    await feed(dp, bot, callback=make_callback("t:lesson", user_id=ADMIN_ID))
    assert (await deps.users.get(ADMIN_ID))["funnel_fast"] == 1
    due = await deps.db.fetchval("SELECT due_at FROM user_steps WHERE user_id = ?", (ADMIN_ID,))
    assert due - (await deps.users.get(ADMIN_ID))["material_sent_at"] <= 15


async def test_role_admin_test_account_is_not_in_stats_or_segments(stack):
    dp, bot, session, deps = stack
    await deps.access.add(600, "Клиент", None, role="admin")
    await deps.users.upsert(600, "c", "Клиент")
    await deps.db.execute("UPDATE users SET material_sent_at = 5 WHERE tg_id = 600")
    assert (await deps.users.stats())["total"] == 0
    assert await deps.users.segment_ids("all") == []


async def test_subscription_screen_has_auto_delivery_button(stack):
    """Автовыдача урока настраивается прямо на экране «Проверка подписки», а не в глубине списка текстов."""
    dp, bot, session, deps = stack
    await feed(dp, bot, callback=make_callback("a:flow:sub", user_id=ADMIN_ID))
    assert "a:set:t:auto_deliver_minutes" in _all_callbacks(session)
    assert "выдаст его сам через 60 мин" in _texts(session)
    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:set:t:auto_deliver_minutes", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("0", user_id=ADMIN_ID, message_id=90))
    assert await deps.settings.get_int("auto_deliver_minutes") == 0
    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:flow:sub", user_id=ADMIN_ID))
    assert "Автовыдача урока: выключена" in _texts(session)
    assert "Автовыдача урока: выкл" in " ".join(
        b.text for r in session.requests if getattr(r, "reply_markup", None) for row in r.reply_markup.inline_keyboard for b in row
    )


CUSTOM_EMOJI_HTML = 'Привет <tg-emoji emoji-id="5368324170671202286">😀</tg-emoji>'


async def test_preview_warns_when_telegram_drops_custom_emoji(stack):
    """MockSession возвращает сообщение без custom_emoji-сущностей — как Telegram для бота без Premium у владельца."""
    dp, bot, session, deps = stack
    step_id = await deps.funnel.add_step(3600, text=CUSTOM_EMOJI_HTML, backfill=False)
    session.requests.clear()
    await feed(dp, bot, callback=make_callback(f"a:fun:prev:{step_id}", user_id=ADMIN_ID))
    texts = [m.text for m in session.calls("SendMessage")]
    assert texts[0] == CUSTOM_EMOJI_HTML
    assert any("Premium" in (t or "") for t in texts[1:])


async def test_preview_of_plain_step_has_no_warning(stack):
    dp, bot, session, deps = stack
    step_id = await deps.funnel.add_step(3600, text="Обычный текст", backfill=False)
    session.requests.clear()
    await feed(dp, bot, callback=make_callback(f"a:fun:prev:{step_id}", user_id=ADMIN_ID))
    assert [m.text for m in session.calls("SendMessage")] == ["Обычный текст"]


async def test_material_and_repeat_start_previews_warn_about_custom_emoji(stack):
    dp, bot, session, deps = stack
    block_id = await deps.material.add_block(text=CUSTOM_EMOJI_HTML)
    session.requests.clear()
    await feed(dp, bot, callback=make_callback(f"a:mat:prev:{block_id}", user_id=ADMIN_ID))
    assert any("Premium" in (m.text or "") for m in session.calls("SendMessage"))
    rst_id = await deps.repeat_start.add_block(text=CUSTOM_EMOJI_HTML)
    session.requests.clear()
    await feed(dp, bot, callback=make_callback(f"a:rst:prev:{rst_id}", user_id=ADMIN_ID))
    assert any("Premium" in (m.text or "") for m in session.calls("SendMessage"))


async def test_emoji_check_reports_missing_premium_and_shows_warning_in_menu(stack):
    dp, bot, session, deps = stack
    session.keep_custom_emoji = False
    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:set:emoji", user_id=ADMIN_ID))
    sent = [m.text for m in session.calls("SendMessage")]
    assert any("<tg-emoji" in (t or "") for t in sent)                      # пробный пост с кастомным эмодзи
    report = " ".join(m.text for m in session.calls("EditMessageText"))
    assert "не работают" in report and "Premium" in report
    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:menu", user_id=ADMIN_ID))
    assert "Кастомные эмодзи не показываются" in session.calls("EditMessageText")[-1].text


async def test_emoji_check_reports_success_and_clears_warning(stack):
    dp, bot, session, deps = stack
    session.keep_custom_emoji = True
    await feed(dp, bot, callback=make_callback("a:set:emoji", user_id=ADMIN_ID))
    report = " ".join(m.text for m in session.calls("EditMessageText"))
    assert "работают" in report and "не работают" not in report
    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:menu", user_id=ADMIN_ID))
    assert "Кастомные эмодзи не показываются" not in session.calls("EditMessageText")[-1].text


async def test_settings_screen_has_emoji_check_button(stack):
    dp, bot, session, deps = stack
    await feed(dp, bot, callback=make_callback("a:set", user_id=ADMIN_ID))
    labels = [b.text for row in session.calls("EditMessageText")[-1].reply_markup.inline_keyboard for b in row]
    assert "✨ Проверка кастомных эмодзи" in labels
