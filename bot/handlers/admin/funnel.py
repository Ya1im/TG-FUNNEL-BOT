"""Редактор цепочки прогрева."""
from __future__ import annotations

import json

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from bot.repo.funnel import block_from_row, human_delay, parse_delay
from bot.sender import send_block
from bot.handlers.admin.common import (
    FunnelAdd,
    FunnelEditContent,
    FunnelEditDelay,
    FunnelImport,
    buttons_hint,
    capture_content,
    kb,
    parse_buttons,
    preview,
    show,
)

router = Router(name="admin-funnel")

HINT = (
    "🔥 <b>Прогрев</b>\n\n"
    "Цепочка сообщений, которая уходит сама по расписанию — отсчитывая время от момента, "
    "когда человек получил материал.\n\n"
    "🔒 — шаг уйдёт только тем, кто подписан на канал (для доступов и закрытых материалов)."
)


async def funnel_screen(target, deps) -> None:
    steps = await deps.funnel.list_steps()
    rows = [[("➕ Добавить шаг", "a:fun:add")]]
    lines = []
    for index, step in enumerate(steps, start=1):
        mark = "🔒" if step["requires_subscription"] else ""
        off = "" if step["enabled"] else "⏸"
        lines.append(
            f"{index}. через {human_delay(step['delay_seconds'])} {mark}{off} — {preview(step['text'])}"
        )
        rows.append([(f"{index}. {human_delay(step['delay_seconds'])} {mark}{off}", f"a:fun:s:{step['id']}")])
    rows.append([("▶️ Прогнать на себе", "a:fun:test")])
    rows.append([("⬇️ Экспорт", "a:fun:exp"), ("⬆️ Импорт", "a:fun:imp")])
    rows.append([("⬅️ Назад", "a:menu")])
    body = "\n".join(lines) if lines else "Шагов пока нет — жми «➕ Добавить шаг»."
    text = HINT + "\n\n" + body
    await show(target, text, kb(rows))


@router.callback_query(F.data == "a:fun")
async def cb_funnel(call: CallbackQuery, deps, state: FSMContext) -> None:
    await state.clear()
    await funnel_screen(call, deps)
    await call.answer()


# --- карточка шага --------------------------------------------------------


@router.callback_query(F.data.startswith("a:fun:s:"))
async def cb_step(call: CallbackQuery, deps) -> None:
    step_id = int(call.data.split(":")[-1])
    await step_screen(call, deps, step_id)
    await call.answer()


async def step_screen(target, deps, step_id: int) -> None:
    steps = {s["id"]: s for s in await deps.funnel.list_steps()}
    step = steps.get(step_id)
    if step is None:
        await show(target, "Шаг не найден.", kb([[("⬅️ К прогреву", "a:fun")]]))
        return
    text = (
        f"🔥 <b>Шаг прогрева</b>\n\n"
        f"⏱ Через: {human_delay(step['delay_seconds'])}\n"
        f"⚡️ Статус: {'включён' if step['enabled'] else 'выключен'}\n"
        f"🔒 Только подписчикам: {'да' if step['requires_subscription'] else 'нет'}\n\n"
        f"🎬 Медиа: {step['media_slug'] or 'нет'}\n"
        f"🔘 Кнопки: {buttons_hint(step['buttons_json'])}\n\n"
        f"Текст:\n{step['text'] or '<i>без текста</i>'}"
    )
    await show(
        target,
        text,
        kb(
            [
                [("⏱ Задержка", f"a:fun:delay:{step_id}"), ("👁 Показать", f"a:fun:prev:{step_id}")],
                [("✏️ Текст/медиа", f"a:fun:ed:{step_id}"), ("🔘 Кнопки", f"a:fun:btn:{step_id}")],
                [
                    ("🔒 Подписка вкл/выкл", f"a:fun:gate:{step_id}"),
                    ("⏸ Вкл/выкл", f"a:fun:tgl:{step_id}"),
                ],
                [("⬆️ Выше", f"a:fun:up:{step_id}"), ("⬇️ Ниже", f"a:fun:dn:{step_id}")],
                [("🗑 Удалить", f"a:fun:del:{step_id}")],
                [("⬅️ К прогреву", "a:fun")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("a:fun:prev:"))
async def cb_step_preview(call: CallbackQuery, deps) -> None:
    step_id = int(call.data.split(":")[-1])
    steps = {s["id"]: s for s in await deps.funnel.list_steps()}
    step = steps.get(step_id)
    if step:
        await send_block(block_from_row(step), call.bot, call.message.chat.id, user=call.from_user)
    await call.answer()


@router.callback_query(F.data.startswith("a:fun:gate:"))
async def cb_step_gate(call: CallbackQuery, deps) -> None:
    step_id = int(call.data.split(":")[-1])
    step = await deps.funnel.get_step(step_id)
    await deps.funnel.update_step(step_id, requires_subscription=0 if step["requires_subscription"] else 1)
    await call.answer("Готово")
    await step_screen(call, deps, step_id)


@router.callback_query(F.data.startswith("a:fun:tgl:"))
async def cb_step_toggle(call: CallbackQuery, deps) -> None:
    step_id = int(call.data.split(":")[-1])
    step = await deps.funnel.get_step(step_id)
    await deps.funnel.update_step(step_id, enabled=0 if step["enabled"] else 1)
    await call.answer("Готово")
    await step_screen(call, deps, step_id)


@router.callback_query(F.data.startswith("a:fun:up:"))
async def cb_step_up(call: CallbackQuery, deps) -> None:
    step_id = int(call.data.split(":")[-1])
    await deps.funnel.move_step(step_id, -1)
    await call.answer()
    await funnel_screen(call, deps)


@router.callback_query(F.data.startswith("a:fun:dn:"))
async def cb_step_down(call: CallbackQuery, deps) -> None:
    step_id = int(call.data.split(":")[-1])
    await deps.funnel.move_step(step_id, 1)
    await call.answer()
    await funnel_screen(call, deps)


@router.callback_query(F.data.startswith("a:fun:del:"))
async def cb_step_delete(call: CallbackQuery, deps) -> None:
    step_id = int(call.data.split(":")[-1])
    await deps.funnel.delete_step(step_id)
    await call.answer("Шаг удалён")
    await funnel_screen(call, deps)


# --- изменение содержимого и кнопок ---------------------------------------


@router.callback_query(F.data.startswith("a:fun:ed:"))
async def cb_step_edit(call: CallbackQuery, state: FSMContext) -> None:
    step_id = int(call.data.split(":")[-1])
    await state.update_data(edit_step_id=step_id)
    await state.set_state(FunnelEditContent.waiting_content)
    await show(
        call,
        "✏️ <b>Новое содержимое шага</b>\n\n"
        "Пришли одним сообщением — заменит и текст, и медиа этого шага целиком.",
        kb([[("⬅️ Отмена", f"a:fun:s:{step_id}")]]),
    )
    await call.answer()


@router.message(FunnelEditContent.waiting_content)
async def on_step_edit_content(message: Message, state: FSMContext, deps) -> None:
    data = await state.get_data()
    step_id = data.get("edit_step_id")
    if step_id is None:
        await state.clear()
        return
    text, media_id = await capture_content(deps, message, "step")
    if not text and not media_id:
        await message.answer("Пустое сообщение. Пришли текст или файл.")
        return
    await deps.funnel.update_step(step_id, text=text, media_id=media_id)
    await state.clear()
    await step_screen(message, deps, step_id)


@router.callback_query(F.data.startswith("a:fun:btn:"))
async def cb_step_edit_buttons(call: CallbackQuery, state: FSMContext) -> None:
    step_id = int(call.data.split(":")[-1])
    await state.update_data(edit_step_id=step_id)
    await state.set_state(FunnelEditContent.waiting_buttons)
    await show(
        call,
        "🔘 <b>Кнопки шага</b>\n\n"
        "Пришли построчно:\n<code>Текст кнопки | https://ссылка</code>\n\n"
        "Чтобы убрать все кнопки — отправь <code>-</code>",
        kb([[("⬅️ Отмена", f"a:fun:s:{step_id}")]]),
    )
    await call.answer()


@router.message(FunnelEditContent.waiting_buttons, F.text)
async def on_step_edit_buttons(message: Message, state: FSMContext, deps) -> None:
    data = await state.get_data()
    step_id = data.get("edit_step_id")
    if step_id is None:
        await state.clear()
        return
    buttons = [] if message.text.strip() == "-" else parse_buttons(message.text)
    await deps.funnel.update_step(step_id, buttons_json=json.dumps(buttons, ensure_ascii=False))
    await state.clear()
    await step_screen(message, deps, step_id)


# --- изменение задержки ---------------------------------------------------


@router.callback_query(F.data.startswith("a:fun:delay:"))
async def cb_step_delay(call: CallbackQuery, state: FSMContext) -> None:
    step_id = int(call.data.split(":")[-1])
    await state.set_state(FunnelEditDelay.waiting_value)
    await state.update_data(step_id=step_id)
    await show(
        call,
        "⏱ <b>Задержка шага</b>\n\n"
        "Через сколько после выдачи материала слать этот шаг?\n\n"
        "Примеры: <code>30м</code>, <code>2ч</code>, <code>3д</code>, <code>1д 4ч</code>.",
        kb([[("⬅️ Отмена", f"a:fun:s:{step_id}")]]),
    )
    await call.answer()


@router.message(FunnelEditDelay.waiting_value, F.text)
async def on_step_delay(message: Message, state: FSMContext, deps) -> None:
    seconds = parse_delay(message.text)
    if seconds is None:
        await message.answer("Не понял. Напиши как <code>2ч</code>, <code>30м</code> или <code>3д</code>.")
        return
    data = await state.get_data()
    await deps.funnel.update_step(data["step_id"], delay_seconds=seconds)
    await state.clear()
    await step_screen(message, deps, data["step_id"])


# --- добавление шага ------------------------------------------------------


@router.callback_query(F.data == "a:fun:add")
async def cb_step_add(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(FunnelAdd.waiting_delay)
    await show(
        call,
        "➕ <b>Новый шаг прогрева</b>\n\n"
        "Через сколько после выдачи материала слать этот шаг?\n\n"
        "Примеры: <code>30м</code>, <code>2ч</code>, <code>3д</code>, <code>1д 4ч</code>.",
        kb([[("⬅️ Отмена", "a:fun")]]),
    )
    await call.answer()


@router.message(FunnelAdd.waiting_delay, F.text)
async def on_add_delay(message: Message, state: FSMContext) -> None:
    seconds = parse_delay(message.text)
    if seconds is None:
        await message.answer("Не понял. Напиши как <code>2ч</code>, <code>30м</code> или <code>3д</code>.")
        return
    await state.update_data(delay=seconds)
    await state.set_state(FunnelAdd.waiting_content)
    await message.answer(
        f"Задержка: {human_delay(seconds)}.\n\n"
        "Теперь пришли сам контент одним сообщением — текст, фото с подписью, видео, кружок или файл."
    )


@router.message(FunnelAdd.waiting_content)
async def on_add_content(message: Message, state: FSMContext, deps) -> None:
    text, media_id = await capture_content(deps, message, "step")
    if not text and not media_id:
        await message.answer("Пустое сообщение. Пришли текст или файл.")
        return
    await state.update_data(text=text, media_id=media_id)
    await state.set_state(FunnelAdd.waiting_buttons)
    await message.answer(
        "Нужны кнопки-ссылки? Пришли построчно в формате\n"
        "<code>Текст кнопки | https://ссылка</code>\n\n"
        "Если кнопки не нужны — отправь <code>-</code>"
    )


@router.message(FunnelAdd.waiting_buttons, F.text)
async def on_add_buttons(message: Message, state: FSMContext, deps) -> None:
    buttons = [] if message.text.strip() == "-" else parse_buttons(message.text)
    data = await state.get_data()
    await deps.funnel.add_step(
        delay_seconds=data["delay"],
        text=data.get("text"),
        media_id=data.get("media_id"),
        buttons=buttons,
    )
    await state.clear()
    await funnel_screen(message, deps)


# --- тестовый прогон, экспорт, импорт -------------------------------------


@router.callback_query(F.data == "a:fun:test")
async def cb_funnel_test(call: CallbackQuery, deps) -> None:
    user_id = call.from_user.id
    await deps.users.upsert(user_id, call.from_user.username, call.from_user.first_name)
    await deps.funnel.clear_user(user_id)
    count = await deps.funnel.enqueue(user_id, fast=True)
    await call.answer()
    await call.message.answer(
        f"Запустил прогон на тебе: {count} шагов с задержкой по 10 секунд.\n"
        "Шаги с 🔒 всё так же проверят подписку."
    )


@router.callback_query(F.data == "a:fun:exp")
async def cb_funnel_export(call: CallbackQuery, deps) -> None:
    raw = await deps.funnel.export_json()
    await call.message.answer_document(
        BufferedInputFile(raw.encode("utf-8"), filename="funnel.json"),
        caption="Цепочка прогрева. Этот файл можно залить обратно через «Импорт».",
    )
    await call.answer()


@router.callback_query(F.data == "a:fun:imp")
async def cb_funnel_import(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(FunnelImport.waiting_file)
    await show(
        call,
        "⬆️ <b>Импорт прогрева</b>\n\n"
        "Пришли файл <code>funnel.json</code>.\n\n"
        "⚠️ Текущая цепочка будет заменена целиком, очередь отправок сбросится.\n\n"
        "Медиа подтянется по именам из медиатеки — залей файлы заранее.",
        kb([[("⬅️ Отмена", "a:fun")]]),
    )
    await call.answer()


@router.message(FunnelImport.waiting_file, F.document)
async def on_funnel_import(message: Message, state: FSMContext, deps) -> None:
    file = await message.bot.get_file(message.document.file_id)
    buffer = await message.bot.download_file(file.file_path)
    try:
        await deps.funnel.import_json(buffer.read().decode("utf-8"), deps.media)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        await message.answer(f"Не смог разобрать файл: {exc}")
        return
    await state.clear()
    await funnel_screen(message, deps)
