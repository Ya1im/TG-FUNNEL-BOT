from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChatMember

from bot.repo.settings import SettingsRepo
from bot.repo.users import UsersRepo
from bot.subscription import ChannelGate, is_member

METHOD = GetChatMember(chat_id=1, user_id=1)


class Member:
    def __init__(self, status, is_member_flag=None):
        self.status = status
        if is_member_flag is not None:
            self.is_member = is_member_flag


class FakeBot:
    def __init__(self, member=None, error=None):
        self.member = member
        self.error = error
        self.calls = []

    async def get_chat_member(self, chat_id, user_id):
        self.calls.append((chat_id, user_id))
        if self.error:
            raise self.error
        return self.member


async def test_member_statuses():
    for status in ("member", "administrator", "creator"):
        assert await is_member(FakeBot(Member(status)), "@ch", 1) is True
    for status in ("left", "kicked"):
        assert await is_member(FakeBot(Member(status)), "@ch", 1) is False


async def test_restricted_depends_on_is_member():
    assert await is_member(FakeBot(Member("restricted", True)), "@ch", 1) is True
    assert await is_member(FakeBot(Member("restricted", False)), "@ch", 1) is False


async def test_api_error_means_not_subscribed():
    bot = FakeBot(error=TelegramBadRequest(method=METHOD, message="user not found"))
    assert await is_member(bot, "@ch", 1) is False


async def test_gate_open_when_channel_not_configured(db):
    gate = ChannelGate(FakeBot(Member("left")), SettingsRepo(db), UsersRepo(db))
    assert await gate.check(1) is True


async def test_gate_updates_cache(db):
    users = UsersRepo(db)
    settings = SettingsRepo(db)
    await users.upsert(1)
    await settings.set("channel_id", "-1001234567890")
    bot = FakeBot(Member("member"))
    gate = ChannelGate(bot, settings, users)

    assert await gate.check(1) is True
    assert (await users.get(1))["is_subscribed"] == 1
    assert bot.calls == [(-1001234567890, 1)]

    gate.bot = FakeBot(Member("left"))
    assert await gate.check(1) is False
    assert (await users.get(1))["is_subscribed"] == 0


async def test_gate_normalizes_username(db):
    settings = SettingsRepo(db)
    users = UsersRepo(db)
    await users.upsert(1)
    await settings.set("channel_id", "mychannel")
    bot = FakeBot(Member("member"))
    await ChannelGate(bot, settings, users).check(1)
    assert bot.calls == [("@mychannel", 1)]
