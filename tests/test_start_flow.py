import pytest

from bot.config import Config
from bot.deps import Deps
from bot.sender import RateLimiter
from bot.services import (
    check_subscription_flow,
    deliver_material,
    migrate_repeat_start_blocks,
    start_flow,
)


class FakeUser:
    def __init__(self, uid=1, username="vasya", first_name="Вася"):
        self.id = uid
        self.username = username
        self.first_name = first_name


class FakeLink:
    invite_link = "https://t.me/+personal"


class FakeBot:
    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.calls.append(("text", text, reply_markup))
        return "msg"

    async def send_video_note(self, chat_id, file_id, reply_markup=None):
        self.calls.append(("video_note", file_id, reply_markup))
        return "note"

    async def send_document(self, chat_id, file_id, caption=None, reply_markup=None):
        self.calls.append(("document", file_id, caption))
        return "doc"

    async def create_chat_invite_link(self, chat_id, name=None, member_limit=None):
        self.calls.append(("invite", chat_id, member_limit))
        return FakeLink()


class FakeGate:
    def __init__(self, ok=True):
        self.ok = ok

    async def check(self, user_id):
        return self.ok


@pytest.fixture
def config():
    return Config(
        bot_token="x", admin_ids=(99,), db_path=":memory:", messages_per_second=0, tick_seconds=60
    )


async def make_deps(db, config, gate=None):
    deps = Deps.build(config, db, bot=None)
    deps.gate = gate or FakeGate()
    deps.limiter = RateLimiter(0)
    return deps


async def test_start_registers_user_and_sends_note_and_menu(db, config):
    deps = await make_deps(db, config)
    media_id = await deps.media.save("krug", "video_note", "FILE_NOTE")
    await deps.settings.set("welcome_note_media_id", str(media_id))
    await deps.settings.set("channel_url", "https://t.me/mychannel")
    bot = FakeBot()

    await start_flow(bot, deps, FakeUser(), chat_id=1, payload="ig_reels")

    kinds = [c[0] for c in bot.calls]
    assert kinds == ["video_note", "text"]
    user = await deps.users.get(1)
    assert user["source"] == "ig_reels"
    kb = bot.calls[1][2]
    assert kb.inline_keyboard[0][0].url == "https://t.me/mychannel"
    assert kb.inline_keyboard[1][0].callback_data == "check_sub"


async def test_start_without_note_sends_only_menu(db, config):
    deps = await make_deps(db, config)
    bot = FakeBot()
    await start_flow(bot, deps, FakeUser(), chat_id=1)
    assert [c[0] for c in bot.calls] == ["text"]


async def test_repeat_start_before_material_skips_note(db, config):
    """Кружок шлём только на самый первый /start — повтор до подписки лишь освежает меню."""
    deps = await make_deps(db, config)
    media_id = await deps.media.save("krug", "video_note", "FILE_NOTE")
    await deps.settings.set("welcome_note_media_id", str(media_id))
    bot = FakeBot()

    await start_flow(bot, deps, FakeUser(), chat_id=1)
    assert [c[0] for c in bot.calls] == ["video_note", "text"]

    bot.calls.clear()
    await start_flow(bot, deps, FakeUser(), chat_id=1)
    assert [c[0] for c in bot.calls] == ["text"]  # кружок не повторяем


async def test_repeat_start_with_no_blocks_sends_nothing(db, config):
    """Раньше на повторный /start уходил один фиксированный текст. Теперь это
    настраиваемые блоки (как у материала) — если админ ни одного не добавил,
    бот на повторный /start ничего не шлёт (это осознанный выбор админа,
    а не баг)."""
    deps = await make_deps(db, config)
    await deps.users.upsert(1)
    await deps.users.mark_material_sent(1)
    bot = FakeBot()
    await start_flow(bot, deps, FakeUser(), chat_id=1)
    assert bot.calls == []


async def test_repeat_start_sends_configured_blocks_in_order(db, config):
    """Повторный /start шлёт ровно те блоки (любого формата), что настроил админ, по порядку."""
    deps = await make_deps(db, config)
    await deps.users.upsert(1)
    await deps.users.mark_material_sent(1)
    doc_id = await deps.media.save("bonus", "document", "FILE_DOC")
    await deps.repeat_start.add_block(text="Привет снова 👋")
    await deps.repeat_start.add_block(text="Вот бонус", media_id=doc_id)
    bot = FakeBot()

    await start_flow(bot, deps, FakeUser(), chat_id=1)

    kinds = [c[0] for c in bot.calls]
    assert kinds == ["text", "document"]
    assert bot.calls[0][1] == "Привет снова 👋"


async def test_repeat_start_skips_disabled_blocks(db, config):
    deps = await make_deps(db, config)
    await deps.users.upsert(1)
    await deps.users.mark_material_sent(1)
    block_id = await deps.repeat_start.add_block(text="Выключенный блок")
    await deps.repeat_start.update_block(block_id, enabled=0)
    await deps.repeat_start.add_block(text="Включённый блок")
    bot = FakeBot()

    await start_flow(bot, deps, FakeUser(), chat_id=1)

    assert len(bot.calls) == 1
    assert bot.calls[0][1] == "Включённый блок"


async def test_migrate_repeat_start_seeds_one_block_from_old_text(db, config):
    """Одноразовая миграция: у уже работающего бота есть старый already_started_text —
    после обновления он должен стать первым (и единственным) блоком, чтобы
    повторный /start не «замолчал» сам по себе."""
    deps = await make_deps(db, config)
    await deps.settings.set("already_started_text", "Ты уже в деле, жди продолжения")

    await migrate_repeat_start_blocks(deps)

    blocks = await deps.repeat_start.list_blocks()
    assert len(blocks) == 1
    assert blocks[0]["text"] == "Ты уже в деле, жди продолжения"


async def test_migrate_repeat_start_runs_only_once(db, config):
    """Если админ после миграции сам удалит все блоки (хочет тишину на повторный
    /start) — повторный запуск бота не должен вернуть старый текст обратно."""
    deps = await make_deps(db, config)
    await migrate_repeat_start_blocks(deps)
    for block in await deps.repeat_start.list_blocks():
        await deps.repeat_start.delete_block(block["id"])

    await migrate_repeat_start_blocks(deps)  # имитируем повторный старт бота

    assert await deps.repeat_start.list_blocks() == []


async def test_check_success_delivers_material_and_starts_funnel(db, config):
    deps = await make_deps(db, config)
    await deps.users.upsert(1, "vasya", "Вася")
    doc_id = await deps.media.save("gaid", "document", "FILE_DOC")
    await deps.material.add_block(text="Вот гайд", media_id=doc_id)
    await deps.material.add_block(text="Смотри видео: https://example.com")
    await deps.funnel.add_step(3600, text="Прогрев 1")
    bot = FakeBot()

    assert await check_subscription_flow(bot, deps, 1, 1) is True
    kinds = [c[0] for c in bot.calls]
    assert kinds == ["text", "document", "text"]  # интро + блоки
    assert (await deps.users.get(1))["material_sent_at"] is not None
    assert await deps.funnel.pending_count(1) == 1


async def test_check_failure_sends_nothing(db, config):
    deps = await make_deps(db, config, gate=FakeGate(ok=False))
    await deps.users.upsert(1)
    await deps.funnel.add_step(3600, text="Прогрев")
    bot = FakeBot()

    assert await check_subscription_flow(bot, deps, 1, 1) is False
    assert bot.calls == []
    assert (await deps.users.get(1))["material_sent_at"] is None
    assert await deps.funnel.pending_count(1) == 0


async def test_material_not_sent_twice(db, config):
    deps = await make_deps(db, config)
    await deps.users.upsert(1)
    await deps.material.add_block(text="Материал")
    bot = FakeBot()
    await check_subscription_flow(bot, deps, 1, 1)
    count = len(bot.calls)
    await check_subscription_flow(bot, deps, 1, 1)
    assert len(bot.calls) == count


async def test_private_invite_link_is_personal(db, config):
    deps = await make_deps(db, config)
    await deps.users.upsert(1)
    await deps.settings.set("private_channel_id", "-1009999999999")
    bot = FakeBot()

    await deliver_material(bot, deps, 1)
    invite_calls = [c for c in bot.calls if c[0] == "invite"]
    assert invite_calls == [("invite", -1009999999999, 1)]
    last_kind, last_text, kb = bot.calls[-1]
    assert kb.inline_keyboard[0][0].url == "https://t.me/+personal"


async def test_invite_falls_back_to_static_link(db, config):
    deps = await make_deps(db, config)
    await deps.users.upsert(1)
    await deps.settings.set("private_invite_link", "https://t.me/+static")
    bot = FakeBot()
    await deliver_material(bot, deps, 1)
    assert bot.calls[-1][2].inline_keyboard[0][0].url == "https://t.me/+static"


async def test_channel_placeholder_is_replaced(db, config):
    deps = await make_deps(db, config)
    await deps.settings.set("channel_url", "https://t.me/nastyacareera")
    await deps.settings.set("menu_text", "Подпишись: {channel}\nПотом жми кнопку")
    bot = FakeBot()
    await start_flow(bot, deps, FakeUser(), chat_id=1)
    assert "https://t.me/nastyacareera" in bot.calls[0][1]
