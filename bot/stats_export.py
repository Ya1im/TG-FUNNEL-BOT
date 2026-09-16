"""Периодическая пересборка сводной .xlsx-таблицы со статистикой.

Файл лежит рядом с базой (data/stats_export.xlsx) и перезаписывается по
расписанию — планировщик дёргает хук на каждом тике, а хук сам решает,
не рано ли ещё (см. bot/backup.py — тот же приём для бэкапов базы).
Админ в таблицу не попадает — он тестер, а не пользователь
(см. UsersRepo._admin_exclusion).
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from openpyxl import Workbook

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_MIN = 60


def export_path(db) -> Path:
    return Path(db.path).parent / "stats_export.xlsx"


async def build_workbook(deps) -> Workbook:
    stats = await deps.users.stats()
    sources = await deps.users.sources()
    rows = await deps.users.export_rows()

    wb = Workbook()
    summary = wb.active
    summary.title = "Сводка"
    summary.append(["Собрано", time.strftime("%Y-%m-%d %H:%M:%S")])
    summary.append([])
    summary.append(["Метрика", "Значение"])
    summary.append(["Всего", stats["total"]])
    summary.append(["Активных", stats["active"]])
    summary.append(["Заблокировали бота", stats["blocked"]])
    summary.append(["Подписаны на канал", stats["subscribed"]])
    summary.append(["Получили материал", stats["got_material"]])
    summary.append(["Пришли за сутки", stats["today"]])
    summary.append([])
    summary.append(["Источник", "Кол-во"])
    for src, cnt in sources:
        summary.append([src, cnt])

    users_sheet = wb.create_sheet("Пользователи")
    users_sheet.append(
        ["tg_id", "username", "имя", "старт", "источник", "статус", "подписан", "материал выдан"]
    )
    for r in rows:
        users_sheet.append(
            [
                r["tg_id"],
                r["username"] or "",
                r["first_name"] or "",
                _fmt(r["started_at"]),
                r["source"] or "",
                r["status"],
                "да" if r["is_subscribed"] else "нет",
                _fmt(r["material_sent_at"]),
            ]
        )
    return wb


def _fmt(ts: int | None) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


async def make_export(deps) -> Path:
    """Пересобрать таблицу и сохранить рядом с базой. Возвращает путь к файлу."""
    wb = await build_workbook(deps)
    target = export_path(deps.db)
    target.parent.mkdir(parents=True, exist_ok=True)
    wb.save(target)
    log.info("Таблица статистики пересобрана: %s", target)
    return target


def stats_export_hook(deps):
    """Хук для планировщика: пересобирает таблицу раз в настроенный интервал.

    Первый вызов всегда пересобирает (state["last"] is None) — чтобы файл
    появился вскоре после старта бота, а не только через целый интервал.
    """
    state = {"last": None}

    async def hook() -> None:
        interval_min = await deps.settings.get_int("stats_export_interval_min")
        if not interval_min or interval_min <= 0:
            interval_min = DEFAULT_INTERVAL_MIN
        now = time.time()
        if state["last"] is not None and now - state["last"] < interval_min * 60:
            return
        state["last"] = now
        try:
            await make_export(deps)
        except Exception:  # noqa: BLE001 — пересборка не должна ронять бота
            log.exception("Не смог пересобрать таблицу статистики")

    return hook
