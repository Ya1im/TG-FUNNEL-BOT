"""Общие зависимости, которые прокидываются в хендлеры."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bot.config import Config
from bot.db import Database
from bot.repo.broadcasts import BroadcastsRepo
from bot.repo.funnel import FunnelRepo
from bot.repo.material import MaterialRepo
from bot.repo.media import MediaRepo
from bot.repo.settings import SettingsRepo
from bot.repo.users import UsersRepo
from bot.sender import RateLimiter
from bot.subscription import ChannelGate


@dataclass
class Deps:
    config: Config
    db: Database
    users: UsersRepo
    media: MediaRepo
    funnel: FunnelRepo
    material: MaterialRepo
    settings: SettingsRepo
    gate: ChannelGate
    limiter: RateLimiter
    broadcasts: Any = None
    engine: Any = None
    scheduler: Any = None

    @classmethod
    def build(cls, config: Config, db: Database, bot) -> "Deps":
        users = UsersRepo(db, admin_ids=config.admin_ids)
        settings = SettingsRepo(db)
        return cls(
            config=config,
            db=db,
            users=users,
            media=MediaRepo(db),
            funnel=FunnelRepo(db, admin_ids=config.admin_ids),
            material=MaterialRepo(db),
            settings=settings,
            gate=ChannelGate(bot, settings, users),
            limiter=RateLimiter(config.messages_per_second),
            broadcasts=BroadcastsRepo(db),
        )
