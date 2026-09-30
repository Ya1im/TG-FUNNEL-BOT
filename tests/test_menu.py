"""Меню команд («кнопка Menu» в Telegram): /start должен быть у всех, включая клиентов со статистикой.

Баг: клиент, зашедший по ссылке доступа, получал личный список из одной /stats;
личный список перекрывает общий, поэтому /start из меню у него пропадал."""
import pytest

from bot.config import Config
from bot.menu import menu_commands, sync_all_menus, sync_menu
from tests.test_start_flow import make_deps


@pytest.fixture
def config():
    return Config(bot_token="x", admin_ids=(99,), db_path=":memory:", messages_per_second=0, tick_seconds=60)


class MenuBot:
    def __init__(self):
        self.set_calls = []     # (chat_id, [команды])
        self.deleted = []       # chat_id

    async def set_my_commands(self, commands, scope=None):
        self.set_calls.append((scope.chat_id, [c.command for c in commands]))

    async def delete_my_commands(self, scope=None):
        self.deleted.append(scope.chat_id)


async def test_owner_menu(db, config):
    deps = await make_deps(db, config)
    assert await menu_commands(deps, 99) == ["start", "admin", "test", "reset"]


async def test_stats_client_keeps_start(db, config):
    deps = await make_deps(db, config)
    await deps.access.add(7, "Борис", None, role="stats")
    assert await menu_commands(deps, 7) == ["start", "stats"]


async def test_role_admin_menu(db, config):
    deps = await make_deps(db, config)
    await deps.access.add(8, "Вика", None, role="admin")
    assert await menu_commands(deps, 8) == ["start", "admin", "test", "stats"]


async def test_listed_tester_menu(db, config):
    deps = await make_deps(db, config)
    await deps.settings.set("preprod_user_ids", "55")
    assert await menu_commands(deps, 55) == ["start", "test"]


async def test_ordinary_user_has_no_personal_menu(db, config):
    deps = await make_deps(db, config)
    assert await menu_commands(deps, 1000) is None


async def test_sync_menu_sets_list_for_client(db, config):
    deps = await make_deps(db, config)
    await deps.access.add(7, "Борис", None, role="stats")
    bot = MenuBot()
    await sync_menu(bot, deps, 7, 7)
    assert bot.set_calls == [(7, ["start", "stats"])]


async def test_sync_menu_clears_stale_list_for_removed_client(db, config):
    """Клиента убрали из доступа — его личный список из /stats должен исчезнуть."""
    deps = await make_deps(db, config)
    bot = MenuBot()
    await sync_menu(bot, deps, 7, 7, clear_if_none=True)
    assert bot.deleted == [7] and bot.set_calls == []


async def test_sync_menu_leaves_ordinary_user_alone_by_default(db, config):
    deps = await make_deps(db, config)
    bot = MenuBot()
    await sync_menu(bot, deps, 1000, 1000)
    assert bot.deleted == [] and bot.set_calls == []


async def test_sync_menu_survives_telegram_errors(db, config):
    deps = await make_deps(db, config)

    class Broken(MenuBot):
        async def set_my_commands(self, commands, scope=None):
            raise RuntimeError("network")

    await sync_menu(Broken(), deps, 99, 99)   # не падает


async def test_sync_all_refreshes_everyone_with_access(db, config):
    """После деплоя меню чинится сразу, не дожидаясь, пока клиент снова напишет /start."""
    deps = await make_deps(db, config)
    await deps.access.add(7, "Борис", None, role="stats")
    await deps.access.add(8, "Вика", None, role="admin")
    await deps.settings.set("preprod_user_ids", "55")
    bot = MenuBot()
    await sync_all_menus(bot, deps, pause=0)
    assert dict(bot.set_calls) == {
        99: ["start", "admin", "test", "reset"],
        7: ["start", "stats"],
        8: ["start", "admin", "test", "stats"],
        55: ["start", "test"],
    }
