"""Шаблон воронки: материал + три прогрева. Запуск: python -m bot.seed

Заполняет структуру только если она пустая — тексты потом правишь в /admin.
"""
from __future__ import annotations

import asyncio

from bot.config import Config
from bot.db import Database
from bot.repo.funnel import FunnelRepo
from bot.repo.material import MaterialRepo

MATERIAL = [
    (
        "🔒 <b>Твой материал</b>\n\n"
        "Здесь текст с описанием урока. Кнопку ниже поменяй на свою ссылку.",
        [{"text": "Смотреть урок", "url": "https://example.com"}],
    ),
]

STEPS = [
    (
        3600,
        "Первый прогрев (через час).\n\nЗамени текст в /admin → 🔥 Прогрев.",
        [{"text": "Смотреть урок", "url": "https://example.com"}],
        False,
    ),
    (
        9000,
        "Второй прогрев (через 2,5 часа).\n\nЗамени текст в /admin → 🔥 Прогрев.",
        [{"text": "Смотреть урок", "url": "https://example.com"}],
        False,
    ),
    (
        14400,
        "Третий прогрев (через 4 часа) — здесь обычно закрытый бонус.\n\n"
        "У этого шага включена проверка подписки 🔒",
        [{"text": "Забрать бонус", "url": "https://example.com"}],
        True,
    ),
]


async def main() -> None:
    config = Config.from_env()
    db = await Database(config.db_path).connect()
    material, funnel = MaterialRepo(db), FunnelRepo(db)

    if await material.count() == 0:
        for text, buttons in MATERIAL:
            await material.add_block(text=text, buttons=buttons)
        print(f"Добавил блоков материала: {len(MATERIAL)}")
    else:
        print("Материал уже настроен — не трогаю")

    if not await funnel.list_steps():
        for delay, text, buttons, gated in STEPS:
            await funnel.add_step(
                delay_seconds=delay, text=text, buttons=buttons, requires_subscription=gated
            )
        print(f"Добавил шагов прогрева: {len(STEPS)}")
    else:
        print("Прогрев уже настроен — не трогаю")

    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
