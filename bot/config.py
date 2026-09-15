"""Конфигурация из переменных окружения."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: tuple[int, ...]
    db_path: str
    messages_per_second: float
    tick_seconds: int

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        if env is None:
            load_dotenv(Path(__file__).resolve().parent.parent / ".env")
            env = dict(os.environ)

        token = (env.get("BOT_TOKEN") or "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN не задан — скопируй .env.example в .env и заполни")

        raw_admins = (env.get("ADMIN_IDS") or "").replace(";", ",")
        admins = tuple(int(x) for x in (p.strip() for p in raw_admins.split(",")) if x)
        if not admins:
            raise RuntimeError("ADMIN_IDS не задан — без него админка недоступна")

        return cls(
            bot_token=token,
            admin_ids=admins,
            db_path=env.get("DB_PATH") or "data/bot.db",
            messages_per_second=float(env.get("MESSAGES_PER_SECOND") or 20),
            tick_seconds=int(env.get("TICK_SECONDS") or 60),
        )

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids
