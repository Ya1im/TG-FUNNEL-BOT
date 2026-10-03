"""Единый отчёт со статистикой.

Один сборщик (`collect`) → список секций (`sections`) → два вывода: Telegram-текст (экран в админке и /stats у клиентов —
один и тот же) и лист «Сводка» в .xlsx (см. bot/stats_export.py). Админы-тестеры в цифры не попадают
(см. UsersRepo._admin_exclusion)."""
from __future__ import annotations

import html
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.repo.funnel import human_delay

TZ = ZoneInfo(os.environ.get("TZ", "Europe/Moscow"))

PLATFORMS = (
    ("instagram", "Instagram"),
    ("tiktok", "TikTok"),
    ("other", "Другие / без метки"),
)
# Первое «слово» метки (деление по _ - . пробелу) → платформа. Ссылки из админки: ?start=ig и ?start=tt.
_TOKENS = {
    "instagram": {"ig", "insta", "inst", "instagram"},
    "tiktok": {"tt", "tik", "tiktok"},
}


def platform_of(source: str | None) -> str:
    """Платформа по метке из ссылки. Пустая и незнакомая метка — «other»."""
    if not source:
        return "other"
    token = re.split(r"[_\-. ]+", source.strip().lower(), maxsplit=1)[0]
    for key, names in _TOKENS.items():
        if token in names:
            return key
    return "other"


def _pct(part: int, total: int) -> int:
    return round(part * 100 / total) if total else 0


def _share(part: int, total: int) -> str:
    return f"{part} ({_pct(part, total)}%)"


def _when(ts: int | None) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M") if ts else "—"


async def collect(deps, now: float | None = None) -> dict:
    """Собирает все цифры отчёта из базы."""
    now = now if now is not None else time.time()
    users, db = deps.users, deps.db
    excl_sql, excl_params = users._admin_exclusion()
    stats = await users.stats()
    total = stats["total"]

    rows = await users.platform_rows()
    bounds = {"d1": int(now) - 86400, "d7": int(now) - 7 * 86400, "d30": int(now) - 30 * 86400}
    platforms = {key: {"key": key, "label": label, "total": 0, "d1": 0, "d7": 0, "d30": 0, "subscribed": 0, "material": 0}
                 for key, label in PLATFORMS}
    inflow = {"d1": 0, "d7": 0, "d30": 0}
    for row in rows:
        item = platforms[platform_of(row["source"])]
        item["total"] += 1
        if row["status"] == "active" and row["is_subscribed"]:
            item["subscribed"] += 1
        if row["material_sent_at"]:
            item["material"] += 1
        for key, border in bounds.items():
            if (row["started_at"] or 0) > border:
                item[key] += 1
                inflow[key] += 1

    tags = [(src, cnt) for src, cnt in await users.sources() if src != "—"][:10]

    steps = await db.fetchall(
        "SELECT fs.position, fs.delay_seconds, "
        " COALESCE(SUM(us.status = 'sent'), 0) AS sent, "
        " COALESCE(SUM(us.status = 'skipped'), 0) AS skipped, "
        " COALESCE(SUM(us.status = 'pending'), 0) AS pending "
        "FROM funnel_steps fs LEFT JOIN user_steps us ON us.step_id = fs.id "
        f"AND us.user_id IN (SELECT tg_id FROM users WHERE {excl_sql}) "
        "WHERE fs.enabled = 1 GROUP BY fs.id ORDER BY fs.position, fs.id",
        excl_params,
    )
    broadcasts = []
    for row in await deps.broadcasts.recent(limit=3):
        broadcasts.append((row, await deps.broadcasts.stats(row["id"])))

    return {
        "now": now,
        "stats": stats,
        "pending": await deps.funnel.pending_count(),
        "inflow": inflow,
        "platforms": list(platforms.values()),
        "tags": tags,
        "steps": [dict(s) for s in steps],
        "broadcasts": broadcasts,
    }


def sections(data: dict) -> list[tuple[str, list[tuple[str, object]]]]:
    """Отчёт как список секций: (заголовок, [(метка, значение)]). Тот же набор идёт и в Telegram, и в Excel."""
    stats, total = data["stats"], data["stats"]["total"]
    out: list[tuple[str, list[tuple[str, object]]]] = [
        ("👥 Люди", [
            ("Всего", total),
            ("Активных", stats["active"]),
            ("Заблокировали бота", stats["blocked"]),
        ]),
        ("🔻 Воронка", [
            ("Запустили бота", total),
            ("Подписаны на канал", _share(stats["subscribed"], total)),
            ("Получили материал", _share(stats["got_material"], total)),
            ("В очереди прогрева", data["pending"]),
        ]),
        ("📈 Приток", [
            ("За 24 часа", data["inflow"]["d1"]),
            ("За 7 дней", data["inflow"]["d7"]),
            ("За 30 дней", data["inflow"]["d30"]),
        ]),
    ]
    platform_rows: list[tuple[str, object]] = []
    for p in data["platforms"]:
        platform_rows += [
            (p["label"], _share(p["total"], total)),
            ("└ за 24 ч · 7 дн · 30 дн", f"{p['d1']} · {p['d7']} · {p['d30']}"),
            ("└ подписались · получили материал",
             f"{_share(p['subscribed'], p['total'])} · {_share(p['material'], p['total'])}"),
        ]
    out.append(("📱 Откуда пришли", platform_rows))
    if data["tags"]:
        out.append(("🔗 Метки ссылок", [(src, _share(cnt, total)) for src, cnt in data["tags"]]))
    if data["steps"]:
        out.append(("🔥 Прогрев", [
            (f"{index}. через {human_delay(s['delay_seconds'])}",
             f"отправлено {s['sent']} · пропущено {s['skipped']} · ждут {s['pending']}")
            for index, s in enumerate(data["steps"], start=1)
        ]))
    if data["broadcasts"]:
        out.append(("📤 Последние рассылки", [
            (f"#{row['id']} {_when(row['scheduled_at'] or row['created_at'])}",
             f"отправлено {st['sent']} · заблокировали {st['blocked']} · ошибок {st['failed']}")
            for row, st in data["broadcasts"]
        ]))
    return out


def render_text(data: dict) -> str:
    stamp = datetime.fromtimestamp(data["now"], TZ).strftime("%d.%m.%Y %H:%M")
    lines = [f"📊 <b>Статистика</b> · {stamp}"]
    for title, rows in sections(data):
        icon, _, name = title.partition(" ")
        lines += ["", f"{icon} <b>{html.escape(name)}</b>"]
        lines += [f"{html.escape(str(label))} — {html.escape(str(value))}" for label, value in rows]
    return "\n".join(lines)


async def build_report(deps, now: float | None = None) -> str:
    return render_text(await collect(deps, now))
