"""Материал, который выдаётся после подтверждения подписки."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.repo.funnel import block_from_row
from bot.sender import send_block
from bot.handlers.admin.common import (
    MaterialAdd,
    buttons_hint,
    capture_content,
    kb,
    parse_buttons,
    preview,
    show,
)

router = Router(name="admin-material")

HINT = (
    "🎁 <b>Материал</b>\n\n"
    "Это то, что человек получает сразу после успешной проверки подписки.\n"
    "Блоки уходят по порядку: файлы, видео, тексты со ссылками.\n"
    "Ссылка в закрытый канал добавляется автоматически, если он задан в настройках."
)


async def material_screen(target, deps) -> None:
    blocks = await deps.material.list_blocks()
    rows = [[("➕ Добавить блок", "a:mat:add")]]
    lines = []
    for index, block in enumerate(blocks, start=1):
        media = f" [{block['media_slug']}]" if block["media_slug"] else ""
        lines.append(f"{index}. {preview(block['text'])}{media}")
        rows.append(
            [
                (f"{index}. {preview(block['text'], 20)}", f"a:mat:s:{block['id']}"),
                ("⬆️", f"a:mat:up:{block['id']}"),
                ("🗑", f"a:mat:del:{block['id']}"),
            ]
        )
    rows.append([("👁 Показать целиком", "a:mat:prev")])
    rows.append([("⬅️ Назад", "a:menu")])
    text = HINT + "\n\n" + ("\n".join(lines) if lines else "Блоков пока нет.")
    await show(target, text, kb(rows))


@router.callback_query(F.data == "a:mat")
async def cb_material(call: CallbackQuery, deps, state: FSMContext) -> None:
    await state.clear()
    await material_screen(call, deps)
    await call.answer()


@router.callback_query(F.data == "a:mat:add")
async def cb_material_add(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(MaterialAdd.waiting_content)
    await show(
        call,
        "Пришли блок материала одним сообщением: текст, файл, видео, фото с подписью.",
        kb([[("⬅️ Отмена", "a:mat")]]),
    )
    await call.answer()


@router.message(MaterialAdd.waiting_content)
async def on_material_content(message: Message, state: FSMContext, deps) -> None:
    text, media_id = await capture_content(deps, message, "material")
    if not text and not media_id:
        await message.answer("Пустое сообщение. Пришли текст или файл.")
        return
    await state.update_data(text=text, media_id=media_id)
    await state.set_state(MaterialAdd.waiting_buttons)
    await message.answer(
        "Кнопки-ссылки для этого блока? Построчно:\n"
        "<code>Текст кнопки | https://ссылка</code>\n\n"
        "Не нужны — отправь <code>-</code>"
    )


@router.message(MaterialAdd.waiting_buttons, F.text)
async def on_material_buttons(message: Message, state: FSMContext, deps) -> None:
    buttons = [] if message.text.strip() == "-" else parse_buttons(message.text)
    data = await state.get_data()
    await deps.material.add_block(
        text=data.get("text"), media_id=data.get("media_id"), buttons=buttons
    )
    await state.clear()
    await message.answer("Блок добавлен ✅")
    await material_screen(message, deps)


@router.callback_query(F.data.startswith("a:mat:s:"))
async def cb_material_show(call: CallbackQuery, deps) -> None:
    block_id = int(call.data.split(":")[-1])
    blocks = {b["id"]: b for b in await deps.material.list_blocks()}
    block = blocks.get(block_id)
    if not block:
        await call.answer("Блок не найден", show_alert=True)
        return
    await send_block(block_from_row(block), call.bot, call.message.chat.id, user=call.from_user)
    await call.message.answer(
        f"Кнопки: {buttons_hint(block['buttons_json'])}",
        reply_markup=kb(
            [
                [("⬇️ Ниже", f"a:mat:dn:{block_id}"), ("⬆️ Выше", f"a:mat:up:{block_id}")],
                [("🗑 Удалить", f"a:mat:del:{block_id}")],
                [("⬅️ К материалу", "a:mat")],
            ]
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith("a:mat:up:"))
async def cb_material_up(call: CallbackQuery, deps) -> None:
    await deps.material.move_block(int(call.data.split(":")[-1]), -1)
    await call.answer()
    await material_screen(call, deps)


@router.callback_query(F.data.startswith("a:mat:dn:"))
async def cb_material_down(call: CallbackQuery, deps) -> None:
    await deps.material.move_block(int(call.data.split(":")[-1]), 1)
    await call.answer()
    await material_screen(call, deps)


@router.callback_query(F.data.startswith("a:mat:del:"))
async def cb_material_delete(call: CallbackQuery, deps) -> None:
    await deps.material.delete_block(int(call.data.split(":")[-1]))
    await call.answer("Удалил")
    await material_screen(call, deps)


@router.callback_query(F.data == "a:mat:prev")
async def cb_material_preview(call: CallbackQuery, deps) -> None:
    await call.answer("Отправляю материал тебе")
    user_id = call.from_user.id
    await deps.users.upsert(user_id, call.from_user.username, call.from_user.first_name)
    intro = (await deps.settings.get("material_intro")).strip()
    if intro:
        await call.message.answer(intro)
    for row in await deps.material.list_blocks(only_enabled=True):
        block = block_from_row(row)
        if not block.is_empty:
            await send_block(block, call.bot, call.message.chat.id, user=call.from_user)
