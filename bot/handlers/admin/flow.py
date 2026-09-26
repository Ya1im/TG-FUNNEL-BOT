"""«Воронка»: путь подписчика по шагам — приветствие, подписка, материал, прогрев, повторный /start."""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from bot.handlers.admin.common import kb, preview, screen_text, show
from bot.repo.media import KIND_TITLES

router = Router(name="admin-flow")

OK, WARN, QUIET = "✅", "⚠️", "➖"


def _plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


async def flow_status(deps) -> list[dict]:
    """Состояние каждого шага пути подписчика: иконка + одна строка + нужен ли он для запуска."""
    settings = deps.settings
    note_id = (await settings.get("welcome_note_media_id")).strip()
    note = await deps.media.get(int(note_id)) if note_id.isdigit() else None
    channel = (await settings.get("channel_id")).strip()
    material = len(await deps.material.list_blocks(only_enabled=True))
    steps = len(await deps.funnel.list_steps(only_enabled=True))
    repeat = len(await deps.repeat_start.list_blocks(only_enabled=True))
    return [
        {
            "key": "hello", "cb": "a:flow:hi", "title": "Приветствие", "icon": "👋", "required": True,
            "ok": note is not None,
            "note": html.escape(f"{KIND_TITLES.get(note['kind'], note['kind'])}: {note['slug']}") if note else "медиа не выбрано",
        },
        {
            "key": "sub", "cb": "a:flow:sub", "title": "Проверка подписки", "icon": "📢", "required": True,
            "ok": bool(channel),
            "note": html.escape((await settings.get("channel_title")).strip() or channel or "канал не задан"),
        },
        {
            "key": "material", "cb": "a:mat", "title": "Материал", "icon": "🎁", "required": True,
            "ok": material > 0,
            "note": f"{material} {_plural(material, 'блок', 'блока', 'блоков')}" if material else "нет блоков",
        },
        {
            "key": "funnel", "cb": "a:fun", "title": "Прогрев", "icon": "🔥", "required": True,
            "ok": steps > 0,
            "note": f"{steps} {_plural(steps, 'шаг', 'шага', 'шагов')}" if steps else "нет шагов",
        },
        {
            "key": "repeat", "cb": "a:rst", "title": "Повторный /start", "icon": "🔁", "required": False,
            "ok": repeat > 0,
            "note": f"{repeat} {_plural(repeat, 'блок', 'блока', 'блоков')}" if repeat else "бот молчит",
        },
    ]


def status_icon(item: dict) -> str:
    if item["ok"]:
        return OK
    return WARN if item["required"] else QUIET


async def flow_screen(target, deps) -> None:
    items = await flow_status(deps)
    lines = [
        f"{index}. {item['icon']} {item['title']} — {status_icon(item)} {item['note']}"
        for index, item in enumerate(items, start=1)
    ]
    rows = [[(f"{index}. {item['icon']} {item['title']}", item["cb"])] for index, item in enumerate(items, start=1)]
    rows.append([("⬅️ Назад", "a:menu")])
    await show(
        target,
        screen_text("🧭 Воронка", "Путь человека от первого /start. Настраивай по порядку сверху вниз.", "\n".join(lines)),
        kb(rows),
    )


@router.callback_query(F.data == "a:flow")
async def cb_flow(call: CallbackQuery, deps, state: FSMContext) -> None:
    await state.clear()
    await flow_screen(call, deps)
    await call.answer()


# --- 1. приветствие -----------------------------------------------------------


async def hello_screen(target, deps) -> None:
    item = (await flow_status(deps))[0]
    menu = preview(await deps.settings.get("menu_text"), 120)
    body = f"{status_icon(item)} Медиа: {item['note']}\n📝 Текст меню: {menu}"
    await show(
        target,
        screen_text("👋 Приветствие", "Первое, что видит человек после /start: кружок и текст с кнопками.", body),
        kb(
            [
                [("🎬 Выбрать кружок/медиа", "a:set:note")],
                [("✏️ Текст меню", "a:flow:hi:txt")],
                [("⬅️ Назад", "a:flow")],
            ]
        ),
    )


@router.callback_query(F.data == "a:flow:hi")
async def cb_hello(call: CallbackQuery, deps, state: FSMContext) -> None:
    await state.clear()
    await hello_screen(call, deps)
    await call.answer()


# --- 2. проверка подписки ---------------------------------------------------------


async def subscription_screen(target, deps) -> None:
    settings = deps.settings
    channel = (await settings.get("channel_id")).strip()
    title = html.escape((await settings.get("channel_title")).strip())
    url = html.escape((await settings.get("channel_url")).strip())
    private = (await settings.get("private_channel_id")).strip()
    body = (
        f"{OK if channel else WARN} Канал: {('<code>%s</code>' % channel) if channel else 'не задан'}"
        f"{' — ' + title if title else ''}\n"
        f"🔗 Ссылка: {url or 'нет'}\n"
        f"🔒 Закрытый канал: {('<code>%s</code>' % private) if private else 'не задан'}"
    )
    await show(
        target,
        screen_text(
            "📢 Проверка подписки",
            "Материал получают только подписанные. Бот должен быть админом канала.",
            body,
        ),
        kb(
            [
                [("📢 Канал для проверки", "a:set:channel")],
                [("🔒 Закрытый канал", "a:set:private")],
                [("✏️ Тексты и кнопки", "a:set:texts:sub")],
                [("⬅️ Назад", "a:flow")],
            ]
        ),
    )


@router.callback_query(F.data == "a:flow:sub")
async def cb_subscription(call: CallbackQuery, deps, state: FSMContext) -> None:
    await state.clear()
    await subscription_screen(call, deps)
    await call.answer()
