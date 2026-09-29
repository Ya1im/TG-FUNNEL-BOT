"""Автовыдача урока по таймеру тем, кто не подписался/не нажал «Проверить»."""
import asyncio

import pytest

from bot.config import Config
from bot.deps import Deps
from bot.sender import RateLimiter
from bot.services import auto_deliver_due, check_subscription_flow, deliver_material_once

T0 = 1_000_000


class FakeLink:
    invite_link = "https://t.me/+personal"


class FakeBot:
    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.calls.append(("text", chat_id, text))
        return "msg"

    async def create_chat_invite_link(self, chat_id, name=None, member_limit=None):
        self.calls.append(("invite", chat_id, member_limit))
        return FakeLink()


class FakeGate:
    def __init__(self, subscribed=False):
        self.subscribed = subscribed

    async def check(self, user_id):
        return self.subscribed

    async def status(self, user_id, cached_seconds=0):
        return "yes" if self.subscribed else "no"


@pytest.fixture
def config():
    return Config(bot_token="x", admin_ids=(99,), db_path=":memory:", messages_per_second=0, tick_seconds=60)


async def make_deps(db, config, subscribed=False):
    deps = Deps.build(config, db, bot=None)
    deps.gate = FakeGate(subscribed)
    deps.limiter = RateLimiter(0)
    await deps.material.add_block(text="Урок")
    await deps.funnel.add_step(3600, text="Пуш")
    return deps


async def add_user(deps, tg_id=1, started_at=T0, status="active"):
    await deps.users.upsert(tg_id, "u", "В")
    await deps.db.execute(
        "UPDATE users SET started_at = ?, status = ? WHERE tg_id = ?", (started_at, status, tg_id)
    )


def texts(bot):
    return [c[2] for c in bot.calls if c[0] == "text"]


async def test_claim_delivery_is_atomic_and_expires(db):
    from bot.repo.users import UsersRepo

    users = UsersRepo(db)
    await users.upsert(1, "u", "В")
    assert await users.claim_delivery(1, now=100) is True
    assert await users.claim_delivery(1, now=101) is False
    assert await users.claim_delivery(1, now=100 + 599) is False
    assert await users.claim_delivery(1, now=100 + 600) is True  # протухший claim можно занять снова


async def test_claim_delivery_refused_when_material_already_sent(db):
    from bot.repo.users import UsersRepo

    users = UsersRepo(db)
    await users.upsert(1, "u", "В")
    await users.mark_material_sent(1)
    assert await users.claim_delivery(1, now=100) is False


async def test_due_for_auto_delivery_filters(db):
    from bot.repo.users import UsersRepo

    users = UsersRepo(db)
    for uid in (1, 2, 3, 4, 5):
        await users.upsert(uid, "u", "В")
        await db.execute("UPDATE users SET started_at = ? WHERE tg_id = ?", (T0, uid))
    await users.mark_material_sent(2)
    await db.execute("UPDATE users SET status = 'blocked' WHERE tg_id = 3")
    await db.execute("UPDATE users SET status = 'stopped' WHERE tg_id = 4")
    await db.execute("UPDATE users SET started_at = ? WHERE tg_id = 5", (T0 + 3000,))  # ещё рано
    assert await users.due_for_auto_delivery(T0 + 3600, 60) == [1]


async def test_auto_delivery_after_timeout_without_subscription(db, config):
    deps = await make_deps(db, config)
    await add_user(deps)
    bot = FakeBot()

    assert await auto_deliver_due(bot, deps, now=T0 + 3600) == 1

    assert "Урок" in texts(bot)
    user = await deps.users.get(1)
    assert user["material_sent_at"] is not None
    assert await deps.funnel.pending_count(1) == 1     # воронка пошла


async def test_auto_delivery_not_before_timeout(db, config):
    deps = await make_deps(db, config)
    await add_user(deps)
    bot = FakeBot()
    assert await auto_deliver_due(bot, deps, now=T0 + 3599) == 0
    assert bot.calls == []
    assert (await deps.users.get(1))["material_sent_at"] is None


async def test_auto_delivery_disabled_when_zero_or_empty(db, config):
    deps = await make_deps(db, config)
    await add_user(deps)
    bot = FakeBot()
    await deps.settings.set("auto_deliver_minutes", "0")
    assert await auto_deliver_due(bot, deps, now=T0 + 10**6) == 0
    await deps.settings.set("auto_deliver_minutes", "")
    assert await auto_deliver_due(bot, deps, now=T0 + 10**6) == 0
    assert bot.calls == []


async def test_auto_delivery_respects_custom_minutes(db, config):
    deps = await make_deps(db, config)
    await deps.settings.set("auto_deliver_minutes", "5")
    await add_user(deps)
    bot = FakeBot()
    assert await auto_deliver_due(bot, deps, now=T0 + 299) == 0
    assert await auto_deliver_due(bot, deps, now=T0 + 300) == 1


async def test_auto_delivery_skips_blocked_and_already_delivered(db, config):
    deps = await make_deps(db, config)
    await add_user(deps, 1)
    await add_user(deps, 2, status="blocked")
    await add_user(deps, 3)
    await deps.users.mark_material_sent(3)
    bot = FakeBot()
    assert await auto_deliver_due(bot, deps, now=T0 + 7200) == 1
    assert {c[1] for c in bot.calls} == {1}


async def test_auto_delivery_skips_private_invite_for_unsubscribed(db, config):
    deps = await make_deps(db, config, subscribed=False)
    await deps.settings.set("private_channel_id", "-1009999999999")
    await add_user(deps)
    bot = FakeBot()
    await auto_deliver_due(bot, deps, now=T0 + 3600)
    assert not [c for c in bot.calls if c[0] == "invite"]
    assert "Твой личный доступ" not in " ".join(texts(bot))


async def test_auto_delivery_sends_private_invite_for_subscribed(db, config):
    deps = await make_deps(db, config, subscribed=True)
    await deps.settings.set("private_channel_id", "-1009999999999")
    await add_user(deps)
    bot = FakeBot()
    await auto_deliver_due(bot, deps, now=T0 + 3600)
    assert [c for c in bot.calls if c[0] == "invite"]


async def test_subscription_and_tick_deliver_exactly_once(db, config):
    deps = await make_deps(db, config, subscribed=True)
    await add_user(deps)
    bot = FakeBot()

    await asyncio.gather(
        check_subscription_flow(bot, deps, 1, 1),
        auto_deliver_due(bot, deps, now=T0 + 3600),
    )

    assert texts(bot).count("Урок") == 1
    assert await db.fetchval("SELECT COUNT(*) FROM user_steps WHERE user_id = 1") == 1


async def test_tick_then_subscription_does_not_redeliver(db, config):
    deps = await make_deps(db, config, subscribed=False)
    await add_user(deps)
    bot = FakeBot()
    await auto_deliver_due(bot, deps, now=T0 + 3600)
    deps.gate = FakeGate(True)
    assert await check_subscription_flow(bot, deps, 1, 1) is True
    assert texts(bot).count("Урок") == 1


async def test_deliver_once_releases_claim_on_error(db, config, monkeypatch):
    deps = await make_deps(db, config)
    await add_user(deps)

    async def broken(only_enabled=False):
        raise RuntimeError("boom")

    monkeypatch.setattr(deps.material, "list_blocks", broken)
    with pytest.raises(RuntimeError):
        await deliver_material_once(FakeBot(), deps, 1)
    assert (await deps.users.get(1))["deliver_claim_at"] is None
    monkeypatch.undo()
    assert await deliver_material_once(FakeBot(), deps, 1) is True


async def test_auto_delivery_one_failure_does_not_stop_others(db, config, monkeypatch):
    deps = await make_deps(db, config)
    await add_user(deps, 1)
    await add_user(deps, 2)
    real = deps.material.list_blocks
    state = {"first": True}

    async def flaky(only_enabled=False):
        if state["first"]:
            state["first"] = False
            raise RuntimeError("boom")
        return await real(only_enabled=only_enabled)

    monkeypatch.setattr(deps.material, "list_blocks", flaky)
    bot = FakeBot()
    assert await auto_deliver_due(bot, deps, now=T0 + 3600) == 1
    delivered = [u for u in (1, 2) if (await deps.users.get(u))["material_sent_at"] is not None]
    assert len(delivered) == 1
