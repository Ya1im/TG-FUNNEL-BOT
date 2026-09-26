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
        if name == "GetChat":
            chat_id = method.chat_id
            resolved_id = chat_id if isinstance(chat_id, int) else 777
            return Chat(
                id=resolved_id,
                type="private",
                username=chat_id.lstrip("@") if isinstance(chat_id, str) else None,
            )
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
