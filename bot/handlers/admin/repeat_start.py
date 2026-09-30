"""Что бот шлёт на повторный /start — произвольные блоки (как «Материал»)."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.repo.funnel import block_from_row
from bot.handlers.admin.common import (
    RepeatStartAdd,
    buttons_hint,
    capture_content,
    send_preview,
    item_label,
    kb,
    parse_buttons,
    preview,
    safe_excerpt,
    screen_text,
    show,
)

router = Router(name="admin-repeat-start")

HINT = "Что бот делает, когда тот, кто уже получил материал, снова жмёт /start."


async def repeat_start_screen(target, deps) -> None:
    blocks = await deps.repeat_start.list_blocks()
    restart = (await deps.settings.get("repeat_start_mode")).strip() == "restart"
    rows = [[("🔄 Режим: перезапуск воронки" if restart else "🔄 Режим: сообщения", "a:rst:mode")]]
    rows.append([("➕ Добавить блок", "a:rst:add")])
    lines = []
    for index, block in enumerate(blocks, start=1):
        mark = "" if block["enabled"] else " ⏸"
        lines.append(f"{index}. {item_label(block['text'], block['media_kind'])}{mark}")
        rows.append(
            [
                (f"{index}. {item_label(block['text'], block['media_kind'], 16)}", f"a:rst:s:{block['id']}"),
                ("⬆️", f"a:rst:up:{block['id']}"),
                ("🗑", f"a:rst:del:{block['id']}"),
            ]
        )
    if blocks:
        rows.append([("👁 Показать целиком", "a:rst:prev")])
    rows.append([("⬅️ Назад", "a:flow")])
    blocks_text = "\n".join(lines) if lines else "Блоков пока нет — на повторный /start бот молчит."
    if restart:
        mode_text = (
            "🔄 <b>Режим: перезапуск воронки.</b> Повторный /start сбрасывает предыдущее прохождение, и человек идёт "
            "по воронке заново: кружок, проверка подписки, урок и весь прогрев с первого шага, с теми же шагами, "
            "файлами, кнопками и задержками. Блоки ниже в этом режиме не отправляются."
        )
    else:
        mode_text = (
            "💬 <b>Режим: сообщения.</b> На повторный /start бот присылает блоки ниже. "
            "Чтобы запускать воронку заново, переключите режим."
        )
    await show(target, screen_text("🔁 Повторный /start", HINT, f"{mode_text}\n\n{blocks_text}"), kb(rows))


@router.callback_query(F.data == "a:rst:mode")
async def cb_repeat_start_mode(call: CallbackQuery, deps) -> None:
    current = (await deps.settings.get("repeat_start_mode")).strip()
    await deps.settings.set("repeat_start_mode", "message" if current == "restart" else "restart")
    await repeat_start_screen(call, deps)
    await call.answer("Режим изменён")


@router.callback_query(F.data == "a:rst")
async def cb_repeat_start(call: CallbackQuery, deps, state: FSMContext) -> None:
    await state.clear()
    await repeat_start_screen(call, deps)
    await call.answer()


@router.callback_query(F.data == "a:rst:add")
async def cb_repeat_start_add(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(RepeatStartAdd.waiting_content)
    await show(
        call,
        "➕ <b>Новый блок</b>\n\n"
        "Пришли его одним сообщением: текст, файл, видео, фото с подписью — что угодно.",
        kb([[("⬅️ Отмена", "a:rst")]]),
    )
    await call.answer()


@router.message(RepeatStartAdd.waiting_content)
async def on_repeat_start_content(message: Message, state: FSMContext, deps) -> None:
    text, media_id = await capture_content(deps, message, "repeat_start")
    if not text and not media_id:
        await message.answer("Пустое сообщение. Пришли текст или файл.")
        return
    await state.update_data(text=text, media_id=media_id)
    await state.set_state(RepeatStartAdd.waiting_buttons)
    await message.answer(
        "Кнопки-ссылки для этого блока? Построчно:\n"
        "<code>Текст кнопки | https://ссылка</code>\n\n"
        "Не нужны — отправь <code>-</code>"
    )


@router.message(RepeatStartAdd.waiting_buttons, F.text)
async def on_repeat_start_buttons(message: Message, state: FSMContext, deps) -> None:
    buttons = [] if message.text.strip() == "-" else parse_buttons(message.text)
    data = await state.get_data()
    await deps.repeat_start.add_block(
        text=data.get("text"), media_id=data.get("media_id"), buttons=buttons
    )
    await state.clear()
    await repeat_start_screen(message, deps)


async def block_screen(target, deps, block_id: int) -> None:
    """Карточка блока — правим то же сообщение, живое превью шлём только по кнопке «Показать»."""
    blocks = {b["id"]: b for b in await deps.repeat_start.list_blocks()}
    block = blocks.get(block_id)
    if block is None:
        await show(target, "Блок не найден.", kb([[("⬅️ К списку", "a:rst")]]))
        return
    on_off = ("🔕 Выключить", f"a:rst:off:{block_id}") if block["enabled"] else (
        "🔔 Включить", f"a:rst:on:{block_id}"
    )
    text = (
        "🔁 <b>Блок повторного /start</b>\n\n"
        f"🎬 Медиа: {block['media_slug'] or 'нет'}\n"
        f"🔘 Кнопки: {buttons_hint(block['buttons_json'])}\n"
        f"Статус: {'включён' if block['enabled'] else 'выключен'}\n\n"
        f"Текст:\n{safe_excerpt(block['text']) or '<i>без текста</i>'}"
    )
    await show(
        target,
        text,
        kb(
            [
                [("👁 Показать", f"a:rst:prev:{block_id}")],
                [("⬇️ Ниже", f"a:rst:dn:{block_id}"), ("⬆️ Выше", f"a:rst:up:{block_id}")],
                [on_off],
                [("🗑 Удалить", f"a:rst:del:{block_id}")],
                [("⬅️ К списку", "a:rst")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("a:rst:s:"))
async def cb_repeat_start_show(call: CallbackQuery, deps) -> None:
    block_id = int(call.data.split(":")[-1])
    await block_screen(call, deps, block_id)
    await call.answer()


@router.callback_query(F.data.startswith("a:rst:prev:"))
async def cb_repeat_start_item_preview(call: CallbackQuery, deps) -> None:
    """Живое превью одного блока — по кнопке, отдельным сообщением, без падения на битом file_id."""
    block_id = int(call.data.split(":")[-1])
    blocks = {b["id"]: b for b in await deps.repeat_start.list_blocks()}
    block = blocks.get(block_id)
    if not block:
        await call.answer("Блок не найден", show_alert=True)
        return
    [outcome] = await send_preview(call, [block_from_row(block)])
    if not outcome.ok:
        await call.answer(
            "Не смог отправить файл — похоже, он от другого бота (сменился токен). "
            "Удали и залей заново.",
            show_alert=True,
        )
        return
    await call.answer()


@router.callback_query(F.data.startswith("a:rst:on:"))
async def cb_repeat_start_on(call: CallbackQuery, deps) -> None:
    block_id = int(call.data.split(":")[-1])
    await deps.repeat_start.update_block(block_id, enabled=1)
    await call.answer("Включил")
    await block_screen(call, deps, block_id)


@router.callback_query(F.data.startswith("a:rst:off:"))
async def cb_repeat_start_off(call: CallbackQuery, deps) -> None:
    block_id = int(call.data.split(":")[-1])
    await deps.repeat_start.update_block(block_id, enabled=0)
    await call.answer("Выключил")
    await block_screen(call, deps, block_id)


@router.callback_query(F.data.startswith("a:rst:up:"))
async def cb_repeat_start_up(call: CallbackQuery, deps) -> None:
    await deps.repeat_start.move_block(int(call.data.split(":")[-1]), -1)
    await call.answer()
    await repeat_start_screen(call, deps)


@router.callback_query(F.data.startswith("a:rst:dn:"))
async def cb_repeat_start_down(call: CallbackQuery, deps) -> None:
    await deps.repeat_start.move_block(int(call.data.split(":")[-1]), 1)
    await call.answer()
    await repeat_start_screen(call, deps)


@router.callback_query(F.data.startswith("a:rst:del:"))
async def cb_repeat_start_delete(call: CallbackQuery, deps) -> None:
    await deps.repeat_start.delete_block(int(call.data.split(":")[-1]))
    await call.answer("Удалил")
    await repeat_start_screen(call, deps)


@router.callback_query(F.data == "a:rst:prev")
async def cb_repeat_start_preview(call: CallbackQuery, deps) -> None:
    await call.answer("Отправляю тебе, как это увидит пользователь")
    blocks = [block_from_row(row) for row in await deps.repeat_start.list_blocks(only_enabled=True)]
    await send_preview(call, [block for block in blocks if not block.is_empty])
