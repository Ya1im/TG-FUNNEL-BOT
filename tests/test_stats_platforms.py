"""Статистика по платформам (Instagram / TikTok) и единый формат отчёта везде."""
import time

import pytest

from bot.repo.broadcasts import BroadcastsRepo
from bot.repo.funnel import FunnelRepo
from bot.repo.settings import SettingsRepo
from bot.repo.users import UsersRepo
from bot.stats_export import build_workbook
from bot.stats_report import build_report, collect, platform_of, sections


class Deps:
    def __init__(self, db, admin_ids=(99,)):
        self.db = db
        self.users = UsersRepo(db, admin_ids=admin_ids)
        self.funnel = FunnelRepo(db, admin_ids=admin_ids)
        self.settings = SettingsRepo(db)
        self.broadcasts = BroadcastsRepo(db)


@pytest.mark.parametrize("source", ["ig", "IG", "ig_reels", "ig-bio", "insta", "insta_story", "instagram", "Instagram.bio"])
def test_instagram_tags(source):
    assert platform_of(source) == "instagram"


@pytest.mark.parametrize("source", ["tt", "TT", "tt_bio", "tt-video", "tik", "tiktok", "TikTok_1"])
def test_tiktok_tags(source):
    assert platform_of(source) == "tiktok"


@pytest.mark.parametrize("source", [None, "", "  ", "reels", "yt", "igor", "ttl_promo", "v_abc", "—"])
def test_other_tags(source):
    assert platform_of(source) == "other"


async def seed(db):
    deps = Deps(db)
    now = int(time.time())
    people = [  # id, метка, часов назад, подписан, материал
        (1, "ig", 2, True, True),
        (2, "ig_reels", 24 * 3, True, False),
        (3, "ig", 24 * 20, False, False),
        (4, "tt", 5, True, True),
        (5, None, 1, False, False),
        (99, "ig", 1, True, True),   # админ-тестер в цифры не входит
    ]
    for uid, src, hours, subscribed, material in people:
        await deps.users.upsert(uid, f"u{uid}", f"Имя{uid}", source=src)
        await db.execute(
            "UPDATE users SET started_at = ?, is_subscribed = ?, material_sent_at = ? WHERE tg_id = ?",
            (now - hours * 3600, 1 if subscribed else 0, now if material else None, uid),
        )
    return deps, now


async def test_platform_numbers(db):
    deps, now = await seed(db)
    data = await collect(deps, now)
    by_key = {p["key"]: p for p in data["platforms"]}

    ig, tt, other = by_key["instagram"], by_key["tiktok"], by_key["other"]
    assert (ig["total"], tt["total"], other["total"]) == (3, 1, 1)             # админ не считается
    assert (ig["d1"], ig["d7"], ig["d30"]) == (1, 2, 3)                        # 2 ч, 3 дня, 20 дней назад
    assert (ig["subscribed"], ig["material"]) == (2, 1)
    assert (tt["subscribed"], tt["material"]) == (1, 1)
    assert (other["subscribed"], other["material"]) == (0, 0)
    assert sum(p["total"] for p in data["platforms"]) == data["stats"]["total"]


async def test_report_text_shows_platform_block(db):
    deps, now = await seed(db)
    text = await build_report(deps, now)
    assert "📱 <b>Откуда пришли</b>" in text
    assert "Instagram — 3 (60%)" in text
    assert "TikTok — 1 (20%)" in text
    assert "Другие / без метки — 1 (20%)" in text
    assert "подписались · получили материал — 2 (67%) · 1 (33%)" in text     # у Instagram
    assert "за 24 ч · 7 дн · 30 дн — 1 · 2 · 3" in text


async def test_report_without_any_tags_puts_everyone_into_other(db):
    deps = Deps(db)
    for uid in (1, 2, 3):
        await deps.users.upsert(uid, f"u{uid}", "x")
    data = await collect(deps)
    assert {p["key"]: p["total"] for p in data["platforms"]} == {"instagram": 0, "tiktok": 0, "other": 3}
    assert data["tags"] == []                                                   # блок «Метки ссылок» не рисуется
    assert "Метки ссылок" not in await build_report(deps)


async def test_empty_database_percentages_do_not_divide_by_zero(db):
    text = await build_report(Deps(db))
    assert "Instagram — 0 (0%)" in text


async def test_html_in_tag_is_escaped(db):
    deps = Deps(db)
    await deps.users.upsert(1, "u", "x", source="<b>x</b>")
    assert "<b>x</b> —" not in await build_report(deps)


async def test_excel_summary_has_same_sections_in_same_order(db):
    deps, now = await seed(db)
    wb = await build_workbook(deps)
    summary = wb["Сводка"]
    titles = [row[0].value for row in summary.iter_rows() if row[1].value == "Значение"]
    assert titles == [title for title, _ in sections(await collect(deps))]
    values = {row[0].value: row[1].value for row in summary.iter_rows() if row[0].value}
    assert values["Instagram"] == "3 (60%)"


async def test_excel_users_sheet_has_platform_column(db):
    deps, now = await seed(db)
    sheet = (await build_workbook(deps))["Пользователи"]
    header = [c.value for c in sheet[1]]
    col = header.index("платформа") + 1
    got = {sheet.cell(row=r, column=1).value: sheet.cell(row=r, column=col).value for r in range(2, sheet.max_row + 1)}
    assert got == {1: "Instagram", 2: "Instagram", 3: "Instagram", 4: "TikTok", 5: "Другие / без метки"}
