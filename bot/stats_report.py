"""Текстовый отчёт со статистикой — для команды /stats у клиентов и админов."""
from __future__ import annotations

import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.repo.funnel import human_delay

TZ = ZoneInfo(os.environ.get("TZ", "Europe/Moscow"))


def _pct(part: int, total: int) -> int:
    return round(part * 100 / total) if total else 0


def _when(ts: int | None) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M") if ts else "—"


async def build_report(deps, now: float | None = None) -> str:
    """Собирает отчёт по базе. Админы-тестеры в цифры не попадают (см. UsersRepo._admin_exclusion)."""
    now = now if now is not None else time.time()
    users, db = deps.users, deps.db
    excl_sql, excl_params = users._admin_exclusion()
    stats = await users.stats()
    total = stats["total"]

    period = await db.fetchone(
        "SELECT SUM(started_at > ?) AS d1, SUM(started_at > ?) AS d7, SUM(started_at > ?) AS d30 "
        f"FROM users WHERE {excl_sql}",
        (int(now) - 86400, int(now) - 7 * 86400, int(now) - 30 * 86400, *excl_params),
    )
    d1, d7, d30 = (int(period[k] or 0) for k in ("d1", "d7", "d30"))

    lines = [
        f"📊 <b>Статистика</b> · {datetime.fromtimestamp(now, TZ).strftime('%d.%m.%Y %H:%M')}",
        "",
        "👥 <b>Люди</b>",
        f"Всего: {total} · активных: {stats['active']} · заблокировали бота: {stats['blocked']}",
        "",
        "🔻 <b>Воронка</b>",
        f"Запустили бота: {total}",
        f"Подписаны на канал: {stats['subscribed']} ({_pct(stats['subscribed'], total)}%)",
        f"Получили материал: {stats['got_material']} ({_pct(stats['got_material'], total)}%)",
        "",
        "📈 <b>Приток</b>",
        f"За 24 часа: {d1} · за 7 дней: {d7} · за 30 дней: {d30}",
    ]

    sources = (await users.sources())[:10]
    if sources:
        lines += ["", "🔗 <b>Источники</b>"]
        lines += [f"{src} — {cnt} ({_pct(cnt, total)}%)" for src, cnt in sources]

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
    if steps:
        lines += ["", "🔥 <b>Прогрев</b>"]
        for index, s in enumerate(steps, start=1):
            lines.append(
                f"{index}. через {human_delay(s['delay_seconds'])} — отправлено {s['sent']}"
                f" · пропущено {s['skipped']} · ждут {s['pending']}"
            )

    recent = await deps.broadcasts.recent(limit=3)
    if recent:
        lines += ["", "📤 <b>Последние рассылки</b>"]
        for row in recent:
            st = await deps.broadcasts.stats(row["id"])
            lines.append(
                f"#{row['id']} {_when(row['scheduled_at'] or row['created_at'])} — "
                f"отправлено {st['sent']}, заблокировали {st['blocked']}, ошибок {st['failed']}"
            )
    return "\n".join(lines)
