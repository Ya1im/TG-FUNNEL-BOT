"""Ежедневный бэкап базы: копия рядом, 14 последних."""
from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

KEEP = 14


def daily_backup_hook(db, keep: int = KEEP):
    """Возвращает корутину-хук для планировщика: делает копию раз в сутки."""
    state = {"day": None}

    async def hook() -> None:
        today = time.strftime("%Y%m%d")
        if state["day"] == today:
            return
        state["day"] = today
        try:
            await make_backup(db, keep=keep)
        except Exception:  # noqa: BLE001 — бэкап не должен ронять бота
            log.exception("Не смог сделать бэкап базы")

    return hook


async def make_backup(db, keep: int = KEEP) -> Path:
    target_dir = Path(db.path).parent / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{Path(db.path).stem}-{time.strftime('%Y%m%d')}.db"
    if target.exists():
        target.unlink()
    await db.conn.execute(f"VACUUM INTO '{target.as_posix()}'")
    log.info("Бэкап базы: %s", target)

    backups = sorted(target_dir.glob(f"{Path(db.path).stem}-*.db"))
    for old in backups[:-keep]:
        old.unlink(missing_ok=True)
    return target
