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
        if name.startswith("Send"):
            return Message(
                message_id=len(self.requests) + 100,
                date=datetime.now(timezone.utc),
                chat=Chat(id=getattr(method, "chat_id", USER_ID), type="private"),
                from_user=BOT_USER,
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
    dp, bot, session, deps = stack
    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:fun:add", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("2ч", user_id=ADMIN_ID, message_id=30))
    await feed(dp, bot, message=make_message("Текст прогрева", user_id=ADMIN_ID, message_id=31))
    await feed(dp, bot, message=make_message("Канал | https://t.me/x", user_id=ADMIN_ID, message_id=32))

    steps = await deps.funnel.list_steps()
    assert len(steps) == 1
    assert steps[0]["delay_seconds"] == 7200
    assert steps[0]["text"] == "Текст прогрева"
    assert "Канал" in steps[0]["buttons_json"]


async def test_admin_broadcast_end_to_end(stack):
    dp, bot, session, deps = stack
    for uid in (1, 2, 3):
        await deps.users.upsert(uid)

    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:bc:new", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("Привет всем!", user_id=ADMIN_ID, message_id=40))
    await feed(dp, bot, callback=make_callback("a:bc:done", user_id=ADMIN_ID))
    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:bc:seg:all", user_id=ADMIN_ID))

    broadcast = (await deps.broadcasts.recent(1))[0]
    targets = await deps.broadcasts.pending_targets(broadcast["id"])
    assert sorted(targets) == [1, 2, 3]

    stats = await deps.engine.run(broadcast["id"])
    assert stats["sent"] == 3
    copied = session.calls("CopyMessage")
    assert sorted(c.chat_id for c in copied) == [1, 2, 3]


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
