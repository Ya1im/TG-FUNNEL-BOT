"""Настройки бота в виде key-value. Всё, что админ правит из бота."""
from __future__ import annotations

from bot.db import Database

DEFAULTS: dict[str, str] = {
    # Канал, на который проверяем подписку
    "channel_id": "",            # -100... или @username
    "channel_url": "",           # https://t.me/...
    "channel_title": "",
    # Закрытый канал/чат, куда выдаём доступ (опционально)
    "private_channel_id": "",
    "private_invite_link": "",   # запасная общая ссылка, если персональную выдать не вышло
    # Приветствие
    "welcome_note_media_id": "",  # id из медиатеки: кружок или любое медиа
    "menu_text": (
        "🎬 Тебе доступен видео-урок на тему:\n\n"
        "— «Название урока»\n\n"
        "Чтобы получить его, подпишись на мой канал: {channel}\n\n"
        "После того, как подпишешься, жми на кнопку ниже 👇"
    ),
    "btn_subscribe": "Канал",
    "btn_check": "Проверить подписку",
    "not_subscribed_alert": "Подписки не вижу. Подпишись на канал и нажми «Проверить подписку» ещё раз 🙌",
    "subscribed_ok_alert": "Готово! Забирай материал 🎁",
    "material_intro": "Держи материал 🎁",
    "reminder_text": (
        "Следующая часть уже готова, но она только для подписчиков канала.\n"
        "Подпишись и нажми «Проверить подписку» — пришлю сразу 👇"
    ),
    # Повторная проверка подписки: напоминание, которое админ сам включает
    # у шага прогрева или у рассылки (бот сам ничего не напоминает)
    "resub_reminder_text": (
        "Похоже, ты не подписан на канал. Подпишись и нажми «Проверить подписку» — "
        "чтобы не пропустить следующее 👇"
    ),
    "gate_retry_hours": "6",     # через сколько часов повторить напоминание в прогреве
    "gate_max_attempts": "3",    # сколько напоминаний в прогреве, потом шаг пропускаем
    # already_started_text больше не редактируется из админки (заменено блоками
    # «🔁 Повторный /start») — значение оставлено только как источник для
    # одноразовой миграции существующих ботов, см. migrate_repeat_start_blocks()
    "already_started_text": "Ты уже в деле 🙌 Материал выше, продолжение придёт само.",
    "private_text": "Твой личный доступ в закрытый канал (ссылка одноразовая) 👇",
    "btn_private": "🔒 Войти в закрытый канал",
    # Таблица статистики: чат для пересылки и частота пересборки
    "report_chat_id": "",              # чат/канал, куда пересылаем таблицу (📤 Переслать эксперту)
    "stats_export_interval_min": "60",  # раз во сколько минут пересобирать stats_export.xlsx
}


class SettingsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._cache: dict[str, str] = {}

    async def get(self, key: str, default: str | None = None) -> str:
        if key in self._cache:
            return self._cache[key]
        value = await self.db.fetchval("SELECT value FROM settings WHERE key = ?", (key,))
        if value is None:
            value = DEFAULTS.get(key, default if default is not None else "")
        self._cache[key] = value
        return value

    async def set(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._cache[key] = value

    async def get_int(self, key: str) -> int | None:
        raw = (await self.get(key)).strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    async def all(self) -> dict[str, str]:
        rows = await self.db.fetchall("SELECT key, value FROM settings")
        data = dict(DEFAULTS)
        data.update({r["key"]: r["value"] for r in rows})
        return data

    def invalidate(self) -> None:
        self._cache.clear()
