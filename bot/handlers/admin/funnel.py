"""Редактор цепочки прогрева."""
from __future__ import annotations

import asyncio
import html
import json

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from bot.repo.funnel import block_from_item, blocks_from_row, human_delay, parse_delay, step_messages
from bot.sender import send_block
from bot.handlers.admin.common import (
    FunnelAdd,
    FunnelEditContent,
    FunnelEditDelay,
    FunnelImport,
    FunnelMsgAdd,
    FunnelMsgEdit,
    MenuRef,
    buttons_hint,
    capture_content,
    item_label,
    kb,
    parse_buttons,
    preview,
    safe_excerpt,
    screen_text,
    show,
)

router = Router(name="admin-funnel")

HINT = "Каждый пост идёт через своё время после ПРЕДЫДУЩЕГО (первый — после выдачи материала). 🔒 — только подписанным."


async def funnel_screen(target, deps) -> None:
    steps = await deps.funnel.list_steps()
    rows = [[("➕ Добавить шаг", "a:fun:add")]]
    lines = []
    for index, step in enumerate(steps, start=1):
        mark = "🔒" if step["requires_subscription"] else ""
        off = "" if step["enabled"] else "⏸"
        count = len(step_messages(step))
        many = f" · 📨{count}" if count > 1 else ""
        lines.append(
            f"{index}. ⏱ {human_delay(step['delay_seconds'])} {mark}{off} "
            f"{item_label(step['text'], step['media_kind'], 30)}{many}"
        )
        rows.append(
            [(f"{index}. ⏱ {human_delay(step['delay_seconds'])} {mark}{off}".strip(), f"a:fun:s:{step['id']}")]
        )
    rows.append([("🧪 Тестовый прогон", "t:open")])
    rows.append([("🧪 Предпрод", "a:pre"), ("🧯 Отозвать отправленное", "a:rec")])
    rows.append([("⬇️ Экспорт", "a:fun:exp"), ("⬆️ Импорт", "a:fun:imp")])
    rows.append([("⬅️ Назад", "a:flow")])
    body = "\n".join(lines) if lines else "Шагов пока нет — жми «➕ Добавить шаг»."
    await show(target, screen_text("🔥 Прогрев", HINT, body), kb(rows))


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


async def step_screen(target, deps, step_id: int, more: bool = False) -> None:
    step = await deps.funnel.get_step_row(step_id)
    if step is None:
        await show(target, "Шаг не найден.", kb([[("⬅️ К прогреву", "a:fun")]]))
        return
    messages = step_messages(step)
    if len(messages) == 1:
        content = (
            f"🎬 Медиа: {html.escape(step['media_slug'] or 'нет')}\n"
            f"🔘 Кнопки: {html.escape(buttons_hint(step['buttons_json']))}\n\n"
            f"Текст:\n{step['text'] or '<i>без текста</i>'}"
        )
    else:
        content = f"📨 Сообщений в шаге: {len(messages)}\n\n" + "\n".join(
            f"{i}. {item_label(m['text'], m['media_kind'], 40)}" + (f" · 🔘{len(m['buttons'])}" if m["buttons"] else "")
            for i, m in enumerate(messages, start=1)
        )
    text = (
        f"🔥 <b>Шаг прогрева</b>\n\n"
        f"⏱ Через: {human_delay(step['delay_seconds'])} после предыдущего поста\n"
        f"⚡️ Статус: {'включён' if step['enabled'] else 'выключен'}\n"
        f"🔒 Только подписчикам: {'да' if step['requires_subscription'] else 'нет'}\n"
        + (
            f"🔔 Если не подписан: {'напомнить подписаться' if step['on_unsub'] == 'remind' else 'молча пропустить шаг'}\n"
            if step["requires_subscription"]
            else ""
        )
        + "\n"
        + content
    )
    if more:
        markup = kb(
            [
                [("🔒 Подписка вкл/выкл", f"a:fun:gate:{step_id}")],
                [("🔔 Если не подписан: напомнить/пропустить", f"a:fun:unsub:{step_id}")],
                [("⏸ Вкл/выкл шаг", f"a:fun:tgl:{step_id}")],
                [("⬆️ Выше", f"a:fun:up:{step_id}"), ("⬇️ Ниже", f"a:fun:dn:{step_id}")],
                [("🗑 Удалить", f"a:fun:del:{step_id}")],
                [("⬅️ К шагу", f"a:fun:s:{step_id}")],
            ]
        )
    else:
        markup = kb(
            [
                [(f"📨 Сообщения ({len(messages)})", f"a:fun:msgs:{step_id}")],
                [("⏱ Задержка", f"a:fun:delay:{step_id}"), ("👁 Показать", f"a:fun:prev:{step_id}")],
                [("⋯ Ещё", f"a:fun:more:{step_id}")],
                [("⬅️ К прогреву", "a:fun")],
            ]
        )
    await show(target, text, markup)


@router.callback_query(F.data.startswith("a:fun:more:"))
async def cb_step_more(call: CallbackQuery, deps) -> None:
    await step_screen(call, deps, int(call.data.split(":")[-1]), more=True)
    await call.answer()


@router.callback_query(F.data.startswith("a:fun:prev:"))
async def cb_step_preview(call: CallbackQuery, deps) -> None:
    step_id = int(call.data.split(":")[-1])
    step = await deps.funnel.get_step_row(step_id)
    if step:
        for block in blocks_from_row(step):
            await send_block(block, call.bot, call.message.chat.id, user=call.from_user)
    await call.answer()


@router.callback_query(F.data.startswith("a:fun:gate:"))
async def cb_step_gate(call: CallbackQuery, deps) -> None:
    step_id = int(call.data.split(":")[-1])
    step = await deps.funnel.get_step(step_id)
    await deps.funnel.update_step(step_id, requires_subscription=0 if step["requires_subscription"] else 1)
    await call.answer("Готово")
    await step_screen(call, deps, step_id, more=True)


@router.callback_query(F.data.startswith("a:fun:unsub:"))
async def cb_step_unsub(call: CallbackQuery, deps) -> None:
    step_id = int(call.data.split(":")[-1])
    step = await deps.funnel.get_step(step_id)
    await deps.funnel.update_step(step_id, on_unsub="skip" if step["on_unsub"] == "remind" else "remind")
    await call.answer("Готово")
    await step_screen(call, deps, step_id, more=True)


@router.callback_query(F.data.startswith("a:fun:tgl:"))
async def cb_step_toggle(call: CallbackQuery, deps) -> None:
    step_id = int(call.data.split(":")[-1])
    step = await deps.funnel.get_step(step_id)
    await deps.funnel.update_step(step_id, enabled=0 if step["enabled"] else 1)
    await call.answer("Готово")
    await step_screen(call, deps, step_id, more=True)


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
        "Через сколько после ПРЕДЫДУЩЕГО поста слать этот шаг? "
        "У первого шага — после выдачи материала.\n\n"
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
        "Через сколько после ПРЕДЫДУЩЕГО поста слать этот шаг? "
        "У первого шага — после выдачи материала.\n\n"
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
    sent = await message.answer(
        _collect_text("➕ <b>Новый шаг прогрева</b>", f"Задержка: {human_delay(seconds)}.", 0),
        reply_markup=_collect_markup("a:fun:adone", "a:fun"),
    )
    await state.update_data(
        delay=seconds, messages=[], _menu_chat_id=sent.chat.id, _menu_message_id=sent.message_id
    )
    await state.set_state(FunnelAdd.collecting)


def _collect_text(title: str, intro: str, count: int, added_label: str = "Добавлено сообщений") -> str:
    counter = (
        f"{added_label}: {count}\n\nШли ещё или жми «✅ Готово»."
        if count
        else "Шли сообщения — текст, фото, видео, кружок, файл, аудио, голосовое, гифку или стикер. "
        "Можно несколько подряд, они уйдут одно за другим."
    )
    return f"{title}\n\n{intro}\n\n{counter}\n\nКнопки-ссылки повесишь после — у каждого сообщения свои."


def _collect_markup(done_data: str, cancel_data: str | None = None):
    rows = [[("✅ Готово", done_data)]]
    if cancel_data:
        rows.append([("✖️ Отмена", cancel_data)])
    return kb(rows)


async def _item_from_message(deps, message: Message) -> dict | None:
    """Сообщение админа → пункт шага (текст, медиа, кнопки — как в рассылке). None — если пусто."""
    text, media_id = await capture_content(deps, message, "step")
    if not text and not media_id:
        return None
    media_kind = file_id = None
    if media_id:
        media_row = await deps.media.get(media_id)
        if media_row:
            media_kind, file_id = media_row["kind"], media_row["file_id"]
    return {"text": text, "media_kind": media_kind, "file_id": file_id, "buttons": [], "media_id": media_id}


async def _refresh_counter(message: Message, state: FSMContext, text: str, markup) -> None:
    """Счётчик правим в одном и том же сообщении, чтобы чат не зарастал одинаковыми уведомлениями."""
    data = await state.get_data()
    chat_id, message_id = data.get("_menu_chat_id"), data.get("_menu_message_id")
    ref = MenuRef(message.bot, chat_id, message_id) if chat_id and message_id else None
    result = await show(ref or message, text, markup)
    if ref is None and result is not None:
        await state.update_data(_menu_chat_id=result.chat.id, _menu_message_id=result.message_id)


_COLLECT_LOCK = asyncio.Lock()

_EMPTY_HINT = (
    "Такой тип сообщения не поддерживается. Пришли текст, фото, видео, кружок, файл, "
    "аудио, голосовое, гифку или стикер."
)


@router.message(FunnelAdd.collecting, ~F.text.startswith("/"))
async def on_add_collect(message: Message, state: FSMContext, deps) -> None:
    # альбом приходит параллельными апдейтами: замок сохраняет порядок и не даёт затереть друг друга
    async with _COLLECT_LOCK:
        item = await _item_from_message(deps, message)
        if item is None:
            await message.answer(_EMPTY_HINT)
            return
        data = await state.get_data()
        messages = data.get("messages", [])
        messages.append(item)
        await state.update_data(messages=messages)
        await _refresh_counter(
            message, state,
            _collect_text("➕ <b>Новый шаг прогрева</b>", f"Задержка: {human_delay(data['delay'])}.", len(messages)),
            _collect_markup("a:fun:adone", "a:fun"),
        )


@router.callback_query(F.data == "a:fun:adone")
async def cb_add_done(call: CallbackQuery, state: FSMContext, deps) -> None:
    data = await state.get_data()
    messages = data.get("messages") or []
    if not messages or "delay" not in data:
        await call.answer("Сначала пришли хотя бы одно сообщение", show_alert=True)
        return
    first, rest = messages[0], messages[1:]
    await state.clear()  # до создания: двойное нажатие «Готово» не должно создать два шага
    step_id = await deps.funnel.add_step(
        delay_seconds=data["delay"],
        text=first.get("text"),
        media_id=first.get("media_id"),
        extra_messages=rest,
    )
    await messages_screen(call, deps, step_id, note="Шаг создан. Кнопки-ссылки повесь на нужные сообщения.")
    await call.answer()


# --- сообщения шага ---------------------------------------------------------


async def messages_screen(target, deps, step_id: int, note: str = "") -> None:
    step = await deps.funnel.get_step_row(step_id)
    if step is None:
        await show(target, "Шаг не найден.", kb([[("⬅️ К прогреву", "a:fun")]]))
        return
    rows, lines = [], []
    for idx, item in enumerate(step_messages(step)):
        n_buttons = len(item["buttons"])
        hint = f" · 🔘{n_buttons}" if n_buttons else ""
        lines.append(f"{idx + 1}. {item_label(item['text'], item['media_kind'])}{hint}")
        rows.append([(f"{idx + 1}. {item_label(item['text'], item['media_kind'], 24)}{hint}", f"a:fun:m:{step_id}:{idx}")])
    rows.append([("➕ Добавить сообщение", f"a:fun:madd:{step_id}")])
    rows.append([("👁 Показать всё", f"a:fun:prev:{step_id}")])
    rows.append([("⬅️ К шагу", f"a:fun:s:{step_id}")])
    hint = "Сообщения уходят подряд, с паузой около секунды. Кнопки у каждого свои."
    body = "\n".join(lines)
    await show(
        target,
        screen_text("📨 Сообщения шага", (note + " " if note else "") + hint, body),
        kb(rows),
    )


async def message_screen(target, deps, step_id: int, idx: int) -> None:
    step = await deps.funnel.get_step_row(step_id)
    messages = step_messages(step) if step else []
    if not (0 <= idx < len(messages)):
        await show(target, "Сообщение не найдено.", kb([[("⬅️ К списку", f"a:fun:msgs:{step_id}")]]))
        return
    item = messages[idx]
    text = (
        f"📨 <b>Сообщение #{idx + 1}</b> из {len(messages)}\n\n"
        f"🎬 Медиа: {item['media_kind'] or 'нет'}\n"
        f"🔘 Кнопки: {html.escape(', '.join(b.get('text', '') for b in item['buttons'])) or 'нет'}\n\n"
        f"Текст:\n{safe_excerpt(item['text']) or '<i>без текста</i>'}"
    )
    await show(
        target,
        text,
        kb(
            [
                [("👁 Показать", f"a:fun:mp:{step_id}:{idx}")],
                [("✏️ Текст/медиа", f"a:fun:me:{step_id}:{idx}"), ("🔘 Кнопки", f"a:fun:mb:{step_id}:{idx}")],
                [("⬆️ Выше", f"a:fun:mu:{step_id}:{idx}"), ("⬇️ Ниже", f"a:fun:md:{step_id}:{idx}")],
                [("🗑 Удалить", f"a:fun:mx:{step_id}:{idx}")],
                [("⬅️ К списку", f"a:fun:msgs:{step_id}")],
            ]
        ),
    )


def _ids(call: CallbackQuery) -> tuple[int, int]:
    parts = call.data.split(":")
    return int(parts[-2]), int(parts[-1])


async def _load_messages(deps, step_id: int) -> list[dict] | None:
    step = await deps.funnel.get_step_row(step_id)
    return step_messages(step) if step else None


@router.callback_query(F.data.startswith("a:fun:msgs:"))
async def cb_messages(call: CallbackQuery, deps, state: FSMContext) -> None:
    await state.clear()
    await messages_screen(call, deps, int(call.data.split(":")[-1]))
    await call.answer()


@router.callback_query(F.data.startswith("a:fun:m:"))
async def cb_message(call: CallbackQuery, deps, state: FSMContext) -> None:
    await state.clear()
    step_id, idx = _ids(call)
    await message_screen(call, deps, step_id, idx)
    await call.answer()


@router.callback_query(F.data.startswith("a:fun:mp:"))
async def cb_message_preview(call: CallbackQuery, deps) -> None:
    step_id, idx = _ids(call)
    messages = await _load_messages(deps, step_id)
    if not messages or not (0 <= idx < len(messages)):
        await call.answer("Сообщение не найдено", show_alert=True)
        return
    await send_block(block_from_item(messages[idx]), call.bot, call.message.chat.id, user=call.from_user)
    await call.answer("Это увидят люди")


async def _move_message(call: CallbackQuery, deps, direction: int) -> None:
    step_id, idx = _ids(call)

    def move(items: list[dict]):
        new_idx = idx + direction
        if not (0 <= idx < len(items) and 0 <= new_idx < len(items)):
            return False
        items[idx], items[new_idx] = items[new_idx], items[idx]

    await deps.funnel.edit_messages(step_id, move)
    await call.answer()
    await messages_screen(call, deps, step_id)


@router.callback_query(F.data.startswith("a:fun:mu:"))
async def cb_message_up(call: CallbackQuery, deps) -> None:
    await _move_message(call, deps, -1)


@router.callback_query(F.data.startswith("a:fun:md:"))
async def cb_message_down(call: CallbackQuery, deps) -> None:
    await _move_message(call, deps, 1)


@router.callback_query(F.data.startswith("a:fun:mx:"))
async def cb_message_delete(call: CallbackQuery, deps) -> None:
    step_id, idx = _ids(call)
    outcome = {"last": False}

    def delete(items: list[dict]):
        if len(items) <= 1:
            outcome["last"] = True
            return False
        if not (0 <= idx < len(items)):
            return False
        items.pop(idx)

    items = await deps.funnel.edit_messages(step_id, delete)
    if items is None:
        await call.answer("Шаг не найден", show_alert=True)
        return
    if outcome["last"]:
        await call.answer("В шаге должно остаться хотя бы одно сообщение — удали сам шаг", show_alert=True)
        return
    await call.answer("Удалил")
    await messages_screen(call, deps, step_id)


@router.callback_query(F.data.startswith("a:fun:me:"))
async def cb_message_edit(call: CallbackQuery, state: FSMContext) -> None:
    step_id, idx = _ids(call)
    await state.update_data(msg_step_id=step_id, msg_idx=idx)
    await state.set_state(FunnelMsgEdit.waiting_content)
    await show(
        call,
        "✏️ <b>Новое содержимое сообщения</b>\n\nПришли одним сообщением — заменит и текст, и медиа целиком. "
        "Кнопки этого сообщения останутся.",
        kb([[("⬅️ Отмена", f"a:fun:m:{step_id}:{idx}")]]),
    )
    await call.answer()


@router.message(FunnelMsgEdit.waiting_content)
async def on_message_edit_content(message: Message, state: FSMContext, deps) -> None:
    data = await state.get_data()
    step_id, idx = data.get("msg_step_id"), data.get("msg_idx")
    if step_id is None or idx is None:
        await state.clear()
        return
    new_item = await _item_from_message(deps, message)
    if new_item is None:
        await message.answer("Пустое сообщение. Пришли текст или файл.")
        return

    def replace(items: list[dict]):
        if not (0 <= idx < len(items)):
            return False
        new_item["buttons"] = items[idx]["buttons"]  # кнопки при замене содержимого остаются
        items[idx] = new_item

    if await deps.funnel.edit_messages(step_id, replace) is None:
        await state.clear()
        await message.answer("Шаг уже удалён.")
        return
    await state.clear()
    await message_screen(message, deps, step_id, idx)


@router.callback_query(F.data.startswith("a:fun:mb:"))
async def cb_message_buttons(call: CallbackQuery, state: FSMContext) -> None:
    step_id, idx = _ids(call)
    await state.update_data(msg_step_id=step_id, msg_idx=idx)
    await state.set_state(FunnelMsgEdit.waiting_buttons)
    await show(
        call,
        "🔘 <b>Кнопки сообщения</b>\n\nПришли построчно:\n<code>Текст кнопки | https://ссылка</code>\n\n"
        "Чтобы убрать все кнопки — отправь <code>-</code>",
        kb([[("⬅️ Отмена", f"a:fun:m:{step_id}:{idx}")]]),
    )
    await call.answer()


@router.message(FunnelMsgEdit.waiting_buttons, F.text)
async def on_message_buttons(message: Message, state: FSMContext, deps) -> None:
    data = await state.get_data()
    step_id, idx = data.get("msg_step_id"), data.get("msg_idx")
    if step_id is None or idx is None:
        await state.clear()
        return
    buttons = [] if message.text.strip() == "-" else parse_buttons(message.text)

    def set_buttons(items: list[dict]):
        if not (0 <= idx < len(items)):
            return False
        items[idx]["buttons"] = buttons

    if await deps.funnel.edit_messages(step_id, set_buttons) is None:
        await state.clear()
        await message.answer("Шаг уже удалён.")
        return
    await state.clear()
    await message_screen(message, deps, step_id, idx)


@router.callback_query(F.data.startswith("a:fun:madd:"))
async def cb_message_add(call: CallbackQuery, deps, state: FSMContext) -> None:
    step_id = int(call.data.split(":")[-1])
    await state.set_state(FunnelMsgAdd.collecting)
    await state.update_data(
        msg_step_id=step_id, added=0, _menu_chat_id=call.message.chat.id, _menu_message_id=call.message.message_id
    )
    await show(
        call,
        _collect_text("📨 <b>Новые сообщения шага</b>", "", 0),
        _collect_markup(f"a:fun:msgs:{step_id}"),
    )
    await call.answer()


@router.message(FunnelMsgAdd.collecting, ~F.text.startswith("/"))
async def on_message_add(message: Message, state: FSMContext, deps) -> None:
    async with _COLLECT_LOCK:
        data = await state.get_data()
        step_id = data.get("msg_step_id")
        if step_id is None:
            await state.clear()
            return
        item = await _item_from_message(deps, message)
        if item is None:
            await message.answer(_EMPTY_HINT)
            return
        # сохраняем сразу — черновика нет
        if await deps.funnel.edit_messages(step_id, lambda items: items.append(item)) is None:
            await state.clear()
            await message.answer("Шаг уже удалён.")
            return
        added = int(data.get("added", 0)) + 1
        await state.update_data(added=added)
        await _refresh_counter(
            message, state,
            _collect_text("📨 <b>Новые сообщения шага</b>", "", added, "Добавлено в шаг"),
            _collect_markup(f"a:fun:msgs:{step_id}"),
        )


# --- экспорт, импорт -------------------------------------


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
