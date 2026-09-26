"""Доступ «только статистика»: репозиторий зрителей, приглашения, текст отчёта."""
from types import SimpleNamespace

from bot.repo.broadcasts import BroadcastsRepo
from bot.repo.funnel import FunnelRepo
from bot.repo.users import UsersRepo
from bot.repo.viewers import ViewersRepo
from bot.stats_report import build_report


async def test_add_list_remove_viewer(db):
    repo = ViewersRepo(db)
    assert await repo.is_viewer(5) is False
    await repo.add(5, "Анна", "anna")
    await repo.add(5, "Анна Новая", "anna")  # повторное добавление обновляет, а не дублирует
    assert await repo.is_viewer(5) is True
    rows = await repo.list()
    assert [(r["tg_id"], r["name"]) for r in rows] == [(5, "Анна Новая")]
    await repo.remove(5)
    assert await repo.is_viewer(5) is False


async def test_invite_is_single_use_and_makes_viewer(db):
    repo = ViewersRepo(db)
    token = await repo.create_invite(created_by=99)
    assert await repo.redeem(token, 7, "Борис", "boris") is True
    assert await repo.is_viewer(7) is True
    assert await repo.redeem(token, 8, "Вика", None) is False  # уже использована
    assert await repo.is_viewer(8) is False


async def test_invite_expires_and_garbage_is_rejected(db):
    repo = ViewersRepo(db)
    token = await repo.create_invite(created_by=99, now=1000)
    assert await repo.redeem(token, 7, "Борис", None, now=1000 + 8 * 86400) is False
    assert await repo.redeem("nonsense", 7, "Борис", None) is False
    assert await repo.is_viewer(7) is False


def _deps(db):
    return SimpleNamespace(
        users=UsersRepo(db, admin_ids=(99,)),
        funnel=FunnelRepo(db, admin_ids=(99,)),
        broadcasts=BroadcastsRepo(db),
        db=db,
    )


async def test_report_counts_funnel_sources_and_excludes_admin(db):
    deps = _deps(db)
    users = deps.users
    await users.upsert(99, "adm", "Админ", source="test")  # тестер не считается
    for uid, src in ((1, "reels"), (2, "reels"), (3, "tiktok"), (4, None)):
        await users.upsert(uid, f"u{uid}", f"Имя{uid}", source=src)
    await users.set_subscription(1, True)
    await users.set_subscription(2, True)
    await db.execute("UPDATE users SET material_sent_at = 1 WHERE tg_id = 1")
    await users.mark_blocked(4)

    text = await build_report(deps)

    assert "Статистика" in text
    assert "Всего: 4" in text and "заблокировали бота: 1" in text
    assert "Подписаны на канал: 2 (50%)" in text
    assert "Получили материал: 1 (25%)" in text
    assert "reels — 2" in text and "tiktok — 1" in text


async def test_report_shows_warmup_steps_and_recent_broadcasts(db):
    deps = _deps(db)
    await deps.users.upsert(1, "u1", "Имя")
    step = await deps.funnel.add_step(3600, text="Первый")
    await deps.funnel.enqueue(1, now=0)
    await deps.funnel.mark_sent((await db.fetchone("SELECT id FROM user_steps"))["id"])
    bid = await deps.broadcasts.create(99, "all", [{"chat_id": 1, "message_id": 1}])
    await deps.broadcasts.set_targets(bid, [1])
    await deps.broadcasts.mark_target(bid, 1, "sent")
    await deps.broadcasts.set_status(bid, "done")

    text = await build_report(deps)

    assert "Прогрев" in text and "отправлено 1" in text
    assert f"#{bid}" in text and "отправлено 1" in text


async def test_report_is_well_formed_on_empty_database(db):
    text = await build_report(_deps(db))
    assert "Всего: 0" in text and len(text) < 4000
