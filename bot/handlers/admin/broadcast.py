"""Массовые рассылки: сбор сообщений, сегменты, отправка и планирование."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.content import ALBUM_KINDS, ContentBlock, extract_media, source_of
from bot.repo.funnel import human_delay
from bot.handlers.admin.common import (
    BroadcastItemEdit,
    BroadcastNew,
    MenuRef,
    capture_content,
    item_label,
    kb,
    message_text,
    parse_buttons,
    preview,
    require_owner,
    safe_excerpt,
    screen_text,
    send_preview,
    show,
)

from bot.recall import MAX_AGE_SECONDS, run_broadcast_recall

log = logging.getLogger(__name__)
router = Router(name="admin-broadcast")

TZ = ZoneInfo(os.environ.get("TZ", "Europe/Moscow"))
_tasks: set[asyncio.Task] = set()
# Сообщения админа приходят параллельными апдейтами (особенно части альбома): без блокировки
# чтение-правка-запись черновика теряет сообщения (то же решение, что в воронке — funnel._COLLECT_LOCK).
_COLLECT_LOCK = asyncio.Lock()

SEGMENTS = [
    ("all", "Все активные"),
    ("subscribed", "Подписаны на канал"),
    ("not_subscribed", "Не подписаны"),
    ("got_material", "Получили материал"),
    ("no_material", "Ещё не получили материал"),
    ("funnel_done", "✅ Закончили воронку"),
    ("funnel_running", "🔄 В процессе прогрева"),
]
STEP_SEGMENT = "step"   # «ждут пост N»: segment_value — id шага воронки

HINT = "Соберите сообщения любого формата, поправьте, выберите получателей."


def fmt_time(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M")


SUB_MODE_LABELS = {
    "off": "выключена — шлём всем из сегмента",
    "skip": "включена — неподписанным не шлём",
    "remind": "включена — неподписанным шлём напоминание вместо рассылки",
}
NEXT_SUB_MODE = {"off": "skip", "skip": "remind", "remind": "off"}


def _stats_line(stats: dict[str, int]) -> str:
    parts = [f"отправлено {stats['sent']}", f"заблокировали {stats['blocked']}", f"ошибок {stats['failed']}"]
    if stats.get("skipped"):
        parts.append(f"пропущено (не подписаны) {stats['skipped']}")
    if stats.get("reminded"):
        parts.append(f"напоминание получили {stats['reminded']}")
    return ", ".join(parts)


STATUS_LABELS = {
    "draft": "черновик",
    "queued": "запланирована",
    "running": "идёт",
    "done": "завершена",
    "cancelled": "остановлена",
    "recalled": "удалена у получателей",
}


async def broadcast_screen(target, deps) -> None:
    recent = await deps.broadcasts.recent(limit=8)
    lines = []
    rows = []
    for row in recent:
        stats = await deps.broadcasts.stats(row["id"])
        when = fmt_time(row["scheduled_at"] or row["created_at"])
        status = STATUS_LABELS.get(row["status"], row["status"])
        lines.append(
            f"#{row['id']} {when} — {status}: {_stats_line(stats)}"
        )
        rows.append([(f"📨 #{row['id']} · {when} · {status}", f"a:bc:o:{row['id']}")])
    text = screen_text(
        "📤 Рассылки", HINT, "<b>Последние</b> (нажми — открыть, остановить или удалить у получателей):\n" + ("\n".join(lines) if lines else "пока не было")
    )
    rows = [[("➕ Новая рассылка", "a:bc:new")]] + rows + [[("⬅️ Назад", "a:menu")]]
    await show(target, text, kb(rows))


async def segment_label(deps, row) -> str:
    """Название получателей рассылки для карточки; для «ждут пост N» номер берётся из текущей воронки."""
    if row["segment"] != STEP_SEGMENT:
        return dict(SEGMENTS).get(row["segment"], row["segment"])
    for stage in (await deps.users.funnel_stages())["stages"]:
        if str(stage["step_id"]) == str(row["segment_value"]):
            return f"Ждут пост {stage['number']} (через {human_delay(stage['delay_seconds'])})"
    return "Ждут поста воронки (шаг удалён)"


async def broadcast_card(target, deps, broadcast_id: int, is_owner: bool) -> None:
    row = await deps.broadcasts.get(broadcast_id)
    if row is None:
        await broadcast_screen(target, deps)
        return
    status = row["status"]
    stats = await deps.broadcasts.stats(broadcast_id)
    messages = await deps.broadcasts.messages(broadcast_id)
    label = await segment_label(deps, row)
    text = (
        f"📨 <b>Рассылка #{broadcast_id}</b> — {STATUS_LABELS.get(status, status)}\n\n"
        f"Сообщений: {len(messages)}\n"
        f"Получатели: {label} — {stats['total']}\n"
        f"🔔 Проверка подписки: {SUB_MODE_LABELS.get(row['sub_mode'], row['sub_mode'])}\n"
        f"{_stats_line(stats)}"
    )
    if status == "draft":
        text += f"\n\n⏱ Время отправки: ~{_eta(stats['total'], deps)}"
    if status in ("cancelled", "recalled") and stats.get("pending"):
        text += f"\n⏹ Не успели отправить: {stats['pending']}"
    rows = [[("👁 Предпросмотр", f"a:bc:p:{broadcast_id}")]]
    if status in ("draft", "queued"):
        rows.append([("🔔 Проверка подписки: сменить", f"a:bc:sub:{broadcast_id}")])
        rows.append([("🚀 Отправить сейчас", f"a:bc:go:{broadcast_id}")])
        rows.append([("🕓 Запланировать", f"a:bc:sch:{broadcast_id}")])
    if status in ("queued", "running"):
        rows.append([("⏹ Остановить рассылку", f"a:bc:x:{broadcast_id}")])
    if status in ("done", "cancelled") and stats["sent"] > 0:
        rows.append([("🧯 Удалить у получателей", f"a:bc:rc:{broadcast_id}")])
    if is_owner and status != "running":
        # Убрать запись из истории — необратимо и не нужно роли admin для повседневной работы.
        # Сообщения у людей это не трогает — для них есть «Удалить у получателей».
        rows.append([("🗑 Убрать из истории", f"a:bc:del:{broadcast_id}")])
    rows.append([("⬅️ К списку", "a:bc")])
    await show(target, text, kb(rows))


@router.callback_query(F.data.startswith("a:bc:sub:"))
async def cb_sub_mode(call: CallbackQuery, deps) -> None:
    broadcast_id = int(call.data.split(":")[-1])
    row = await deps.broadcasts.get(broadcast_id)
    if row is None or row["status"] not in ("draft", "queued"):
        await call.answer("Эту рассылку уже нельзя менять", show_alert=True)
        return
    await deps.broadcasts.set_sub_mode(broadcast_id, NEXT_SUB_MODE.get(row["sub_mode"], "off"))
    await call.answer("Готово")
    await broadcast_card(call, deps, broadcast_id, deps.config.is_admin(call.from_user.id))


@router.callback_query(F.data.startswith("a:bc:o:"))
async def cb_open(call: CallbackQuery, deps) -> None:
    await broadcast_card(call, deps, int(call.data.split(":")[-1]), deps.config.is_admin(call.from_user.id))
    await call.answer()


@router.callback_query(F.data.startswith("a:bc:del:"))
async def cb_delete_ask(call: CallbackQuery, deps) -> None:
    if not await require_owner(call, deps):
        return
    broadcast_id = int(call.data.split(":")[-1])
    await show(
        call,
        f"🗑 Убрать рассылку #{broadcast_id} из истории? Вернуть запись будет нельзя.\n\n"
        "Сообщения у людей при этом останутся — чтобы удалить их, используйте «🧯 Удалить у получателей».",
        kb([[("🗑 Да, убрать", f"a:bc:delok:{broadcast_id}")], [("✖️ Нет", f"a:bc:o:{broadcast_id}")]]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("a:bc:delok:"))
async def cb_delete_do(call: CallbackQuery, deps) -> None:
    if not await require_owner(call, deps):
        return
    broadcast_id = int(call.data.split(":")[-1])
    if await deps.broadcasts.delete(broadcast_id):
        await call.answer("Убрал из истории")
    else:
        await call.answer("Идущую рассылку убрать нельзя", show_alert=True)
    await broadcast_screen(call, deps)


@router.callback_query(F.data == "a:bc")
async def cb_broadcast(call: CallbackQuery, deps, state: FSMContext) -> None:
    await state.clear()
    await broadcast_screen(call, deps)
    await call.answer()


@router.callback_query(F.data == "a:bc:new")
async def cb_new(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastNew.collecting)
    await state.update_data(
        messages=[], _menu_chat_id=call.message.chat.id, _menu_message_id=call.message.message_id
    )
    await show(
        call,
        "📤 <b>Сбор рассылки</b>\n\n"
        "Шли сообщения — можно несколько подряд.\n\n"
        "Когда закончишь, жми «✅ Готово».",
        kb([[("✅ Готово", "a:bc:done")], [("✖️ Отмена", "a:bc")]]),
    )
    await call.answer()


def _album_part(message: Message) -> dict | None:
    """Часть альбома: тип, file_id и номер исходного сообщения. None — это не файл, который Telegram собирает в альбом."""
    if not message.media_group_id:
        return None
    found = extract_media(message)
    if not found or found[0] not in ALBUM_KINDS:
        return None
    return {"kind": found[0], "file_id": found[1], "message_id": message.message_id}


def _insert_ordered(messages: list[dict], item: dict) -> None:
    """Вставить сообщение в черновик по порядку отправки админом, а не по порядку обработки апдейтов.

    Обработчики идут параллельно, и более раннее сообщение может добраться до черновика позже более
    позднего. Сдвигаем вставку левее только тех, у кого номер исходного сообщения известен и больше."""
    pos = len(messages)
    src = item.get("src_id")
    if src is not None:
        while pos > 0 and (messages[pos - 1].get("src_id") or 0) > src:
            pos -= 1
    messages.insert(pos, item)


def _add_album_part(messages: list[dict], message: Message, part: dict) -> None:
    group_id = message.media_group_id
    existing = next((m for m in messages if m.get("group_id") == group_id), None)
    if existing is None:
        item = {
            "text": None,
            "media_kind": "album",
            "file_id": None,
            "buttons": [],
            "album": [],
            "group_id": group_id,
            "src_id": part["message_id"],
            "src_chat": message.chat.id,
        }
    else:
        item = existing
        messages.remove(existing)
    if all(p["message_id"] != part["message_id"] for p in item["album"]):
        item["album"].append(part)
        item["album"].sort(key=lambda p: p["message_id"])
    item["src_id"] = item["album"][0]["message_id"]
    caption = message_text(message)
    if caption and not item.get("text"):
        item["text"] = caption
    _insert_ordered(messages, item)


@router.message(BroadcastNew.collecting)
async def on_collect(message: Message, state: FSMContext, deps) -> None:
    """Копим сообщения для рассылки. Счётчик правим в одном и том же сообщении,
    чтобы при нескольких подряд присланных постах чат не зарастал одинаковыми уведомлениями.

    Альбом приходит от Telegram отдельным апдейтом на каждый файл — склеиваем их обратно в одно
    сообщение рассылки, иначе людям файлы уйдут по одному."""
    part = _album_part(message)
    item = None
    if part is None:
        text, media_id = await capture_content(deps, message, "broadcast")
        if not text and not media_id:
            await message.answer(
                "Такой тип сообщения пока не поддерживается для рассылки. Пришли текст, фото, "
                "видео, кружок, файл, аудио, голосовое, гифку или стикер."
            )
            return
        media_kind = file_id = None
        if media_id:
            media_row = await deps.media.get(media_id)
            if media_row:
                media_kind, file_id = media_row["kind"], media_row["file_id"]
        item = {
            "text": text, "media_kind": media_kind, "file_id": file_id, "buttons": [],
            "src_id": message.message_id, "src_chat": message.chat.id,
        }
    async with _COLLECT_LOCK:
        data = await state.get_data()
        messages = data.get("messages", [])
        if part is not None:
            _add_album_part(messages, message, part)
        else:
            _insert_ordered(messages, item)
        await state.update_data(messages=messages)
    text_out = f"📤 <b>Сбор рассылки</b>\n\nДобавлено сообщений: {len(messages)}\n\nШли ещё или жми «✅ Готово»."
    markup = kb([[("✅ Готово", "a:bc:done")], [("✖️ Отмена", "a:bc")]])
    chat_id, message_id = data.get("_menu_chat_id"), data.get("_menu_message_id")
    ref = MenuRef(message.bot, chat_id, message_id) if chat_id and message_id else None
    result = await show(ref or message, text_out, markup)
    if ref is None and result is not None:
        await state.update_data(_menu_chat_id=result.chat.id, _menu_message_id=result.message_id)


async def broadcast_draft_screen(target, state: FSMContext) -> None:
    """Список ещё не отправленных сообщений черновика — как экран блоков материала."""
    data = await state.get_data()
    messages = data.get("messages", [])
    rows = []
    lines = []
    for idx, item in enumerate(messages):
        n_buttons = len(item.get("buttons") or [])
        btn_hint = f" · 🔘{n_buttons}" if n_buttons else ""
        if item.get("album"):
            btn_hint = f" · {len(item['album'])} шт." + btn_hint
        lines.append(f"{idx + 1}. {item_label(item.get('text'), item.get('media_kind'))}{btn_hint}")
        rows.append(
            [
                (f"{idx + 1}. {item_label(item.get('text'), item.get('media_kind'), 16)}", f"a:bc:d:{idx}"),
                ("🗑", f"a:bc:ddel:{idx}"),
            ]
        )
    rows.append([("➕ Добавить ещё", "a:bc:more")])
    if messages:
        rows.append([("👁 Предпросмотр всей рассылки", "a:bc:dpall")])
        rows.append([("▶️ Дальше — выбрать получателей", "a:bc:tosend")])
    rows.append([("✖️ Отмена", "a:bc")])
    body = "\n".join(lines) if lines else "Сообщений пока нет — пришли хотя бы одно."
    await show(target, screen_text("📤 Сбор рассылки", "Добавьте сообщения, потом «Дальше».", body), kb(rows))


def _media_desc(item: dict) -> str:
    if item.get("album"):
        return f"альбом, файлов: {len(item['album'])}"
    return item.get("media_kind") or "нет"


async def broadcast_item_screen(target, state: FSMContext, idx: int) -> None:
    data = await state.get_data()
    messages = data.get("messages", [])
    if not (0 <= idx < len(messages)):
        await show(target, "Сообщение не найдено.", kb([[("⬅️ К списку", "a:bc:list")]]))
        return
    item = messages[idx]
    text = (
        f"📤 <b>Сообщение #{idx + 1}</b>\n\n"
        f"🎬 Медиа: {_media_desc(item)}\n"
        f"🔘 Кнопки: {', '.join(b['text'] for b in item.get('buttons') or []) or 'нет'}\n\n"
        f"Текст:\n{safe_excerpt(item.get('text')) or '<i>без текста</i>'}"
    )
    await show(
        target,
        text,
        kb(
            [
                [("👁 Показать", f"a:bc:dprev:{idx}")],
                [("✏️ Текст/медиа", f"a:bc:dedit:{idx}"), ("🔘 Кнопки", f"a:bc:dbtn:{idx}")],
                [("⬆️ Выше", f"a:bc:dup:{idx}"), ("⬇️ Ниже", f"a:bc:ddn:{idx}")],
                [("🗑 Удалить", f"a:bc:ddel:{idx}")],
                [("⬅️ К списку", "a:bc:list")],
            ]
        ),
    )


@router.callback_query(F.data == "a:bc:done")
async def cb_done(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("messages"):
        await call.answer("Сначала пришли хотя бы одно сообщение", show_alert=True)
        return
    await broadcast_draft_screen(call, state)
    await call.answer()


@router.callback_query(F.data == "a:bc:list")
async def cb_draft_list(call: CallbackQuery, state: FSMContext) -> None:
    await broadcast_draft_screen(call, state)
    await call.answer()


@router.callback_query(F.data == "a:bc:more")
async def cb_draft_more(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastNew.collecting)
    await show(
        call,
        "📤 <b>Сбор рассылки</b>\n\nШли ещё сообщения — можно несколько подряд.\n\nКогда закончишь, жми «✅ Готово».",
        kb([[("✅ Готово", "a:bc:done")], [("✖️ Отмена", "a:bc")]]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("a:bc:d:"))
async def cb_draft_item(call: CallbackQuery, state: FSMContext) -> None:
    idx = int(call.data.split(":")[-1])
    await broadcast_item_screen(call, state, idx)
    await call.answer()


@router.callback_query(F.data.startswith("a:bc:dprev:"))
async def cb_draft_preview(call: CallbackQuery, state: FSMContext) -> None:
    idx = int(call.data.split(":")[-1])
    data = await state.get_data()
    messages = data.get("messages", [])
    if not (0 <= idx < len(messages)):
        await call.answer("Сообщение не найдено", show_alert=True)
        return
    await send_preview(call, [_block_from_item(messages[idx])])
    await call.answer("Это увидят люди")


@router.callback_query(F.data == "a:bc:dpall")
async def cb_draft_preview_all(call: CallbackQuery, state: FSMContext) -> None:
    """Вся рассылка целиком, в том порядке и виде, в каком её получат люди."""
    messages = (await state.get_data()).get("messages", [])
    if not messages:
        await call.answer("Сначала пришли хотя бы одно сообщение", show_alert=True)
        return
    await call.answer("Сейчас пришлю всё как увидят люди")
    await send_preview(call, [_block_from_item(item) for item in messages])
    await call.message.answer(
        f"👆 Так рассылка выглядит у людей: сообщений — {len(messages)}. Всё верно — жми «▶️ Дальше», "
        "нет — поправь или удали сообщения в списке выше.",
    )


@router.callback_query(F.data.startswith("a:bc:ddel:"))
async def cb_draft_delete(call: CallbackQuery, state: FSMContext) -> None:
    idx = int(call.data.split(":")[-1])
    data = await state.get_data()
    messages = data.get("messages", [])
    if 0 <= idx < len(messages):
        messages.pop(idx)
        await state.update_data(messages=messages)
    await call.answer("Удалил")
    await broadcast_draft_screen(call, state)


@router.callback_query(F.data.startswith("a:bc:dup:"))
async def cb_draft_up(call: CallbackQuery, state: FSMContext) -> None:
    await _move_draft_item(state, int(call.data.split(":")[-1]), -1)
    await call.answer()
    await broadcast_draft_screen(call, state)


@router.callback_query(F.data.startswith("a:bc:ddn:"))
async def cb_draft_down(call: CallbackQuery, state: FSMContext) -> None:
    await _move_draft_item(state, int(call.data.split(":")[-1]), 1)
    await call.answer()
    await broadcast_draft_screen(call, state)


async def _move_draft_item(state: FSMContext, idx: int, direction: int) -> None:
    data = await state.get_data()
    messages = data.get("messages", [])
    new_idx = idx + direction
    if 0 <= idx < len(messages) and 0 <= new_idx < len(messages):
        messages[idx], messages[new_idx] = messages[new_idx], messages[idx]
        await state.update_data(messages=messages)


@router.callback_query(F.data.startswith("a:bc:dedit:"))
async def cb_draft_edit(call: CallbackQuery, state: FSMContext) -> None:
    idx = int(call.data.split(":")[-1])
    messages = (await state.get_data()).get("messages", [])
    if 0 <= idx < len(messages) and messages[idx].get("album"):
        await call.answer("Альбом нельзя править по частям — удалите его (🗑) и пришлите альбом заново.", show_alert=True)
        return
    await state.update_data(edit_idx=idx)
    await state.set_state(BroadcastItemEdit.waiting_content)
    await show(
        call,
        "✏️ <b>Новое содержимое сообщения</b>\n\nПришли одним сообщением — заменит и текст, и медиа целиком.",
        kb([[("⬅️ Отмена", f"a:bc:d:{idx}")]]),
    )
    await call.answer()


@router.message(BroadcastItemEdit.waiting_content)
async def on_draft_edit_content(message: Message, state: FSMContext, deps) -> None:
    data = await state.get_data()
    idx = data.get("edit_idx")
    messages = data.get("messages", [])
    if idx is None or not (0 <= idx < len(messages)):
        await state.set_state(BroadcastNew.collecting)
        return
    if message.media_group_id:
        await message.answer(
            "Альбом одним сообщением не заменить. Пришли один файл или текст, а альбом добавь "
            "отдельно: «➕ Добавить ещё»."
        )
        return
    text, media_id = await capture_content(deps, message, "broadcast")
    if not text and not media_id:
        await message.answer("Пустое сообщение. Пришли текст или файл.")
        return
    media_kind = file_id = None
    if media_id:
        media_row = await deps.media.get(media_id)
        if media_row:
            media_kind, file_id = media_row["kind"], media_row["file_id"]
    messages[idx] = {
        "text": text,
        "media_kind": media_kind,
        "file_id": file_id,
        "buttons": messages[idx].get("buttons", []),
    }
    await state.update_data(messages=messages)
    await state.set_state(BroadcastNew.collecting)
    await broadcast_item_screen(message, state, idx)


@router.callback_query(F.data.startswith("a:bc:dbtn:"))
async def cb_draft_buttons(call: CallbackQuery, state: FSMContext) -> None:
    idx = int(call.data.split(":")[-1])
    messages = (await state.get_data()).get("messages", [])
    if 0 <= idx < len(messages) and messages[idx].get("album"):
        await call.answer("У альбома кнопки не поддерживает сам Telegram. Добавьте кнопку отдельным сообщением после альбома.", show_alert=True)
        return
    await state.update_data(edit_idx=idx)
    await state.set_state(BroadcastItemEdit.waiting_buttons)
    await show(
        call,
        "🔘 <b>Кнопки сообщения</b>\n\nПришли построчно:\n<code>Текст кнопки | https://ссылка</code>\n\n"
        "Чтобы убрать все кнопки — отправь <code>-</code>",
        kb([[("⬅️ Отмена", f"a:bc:d:{idx}")]]),
    )
    await call.answer()


@router.message(BroadcastItemEdit.waiting_buttons, F.text)
async def on_draft_buttons(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    idx = data.get("edit_idx")
    messages = data.get("messages", [])
    if idx is None or not (0 <= idx < len(messages)):
        await state.set_state(BroadcastNew.collecting)
        return
    buttons = [] if message.text.strip() == "-" else parse_buttons(message.text)
    messages[idx]["buttons"] = buttons
    await state.update_data(messages=messages)
    await state.set_state(BroadcastNew.collecting)
    await broadcast_item_screen(message, state, idx)


@router.callback_query(F.data == "a:bc:tosend")
async def cb_pick_segment(call: CallbackQuery, deps, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("messages"):
        await call.answer("Сначала пришли хотя бы одно сообщение", show_alert=True)
        return
    rows = []
    for key, label in SEGMENTS:
        count = await deps.users.segment_count(key)
        rows.append([(f"{label} — {count}", f"a:bc:seg:{key}")])
    rows.append([("🪜 По шагам воронки", "a:bc:stages")])
    rows.append([("✖️ Отмена", "a:bc")])
    await show(
        call,
        "📤 <b>Кому отправляем?</b>\n\nВыбери сегмент получателей.\n"
        "<i>«Закончили воронку» — получили материал и все посты прогрева, ждать им больше нечего. "
        "Список получателей фиксируется в момент выбора.</i>",
        kb(rows),
    )
    await call.answer()


@router.callback_query(F.data == "a:bc:stages")
async def cb_pick_stage(call: CallbackQuery, deps, state: FSMContext) -> None:
    """Выбор получателей по шагу воронки: кто сейчас ждёт пост N."""
    if not (await state.get_data()).get("messages"):
        await call.answer("Черновик потерялся, начни заново", show_alert=True)
        return
    stages = (await deps.users.funnel_stages())["stages"]
    rows = [
        [(f"Ждут пост {s['number']} (через {human_delay(s['delay_seconds'])}) — {s['count']}", f"a:bc:stp:{s['step_id']}")]
        for s in stages
        if s["count"]
    ]
    rows.append([("⬅️ К выбору получателей", "a:bc:tosend")])
    text = "🪜 <b>По шагам воронки</b>\n\nВыбери, кому отправить: тем, кто сейчас ждёт определённый пост прогрева."
    if len(rows) == 1:
        text += "\n\nСейчас никто не ждёт постов прогрева."
    await show(call, text, kb(rows))
    await call.answer()


@router.callback_query(F.data.startswith("a:bc:stp:"))
async def cb_stage(call: CallbackQuery, deps, state: FSMContext) -> None:
    await _create_broadcast(call, deps, state, STEP_SEGMENT, call.data.split(":")[-1])


@router.callback_query(F.data.startswith("a:bc:seg:"))
async def cb_segment(call: CallbackQuery, deps, state: FSMContext) -> None:
    await _create_broadcast(call, deps, state, call.data.split(":")[-1])


async def _create_broadcast(
    call: CallbackQuery, deps, state: FSMContext, segment: str, segment_value: str | None = None
) -> None:
    data = await state.get_data()
    messages = data.get("messages") or []
    if not messages:
        await call.answer("Черновик потерялся, начни заново", show_alert=True)
        return
    broadcast_id, total = await deps.engine.prepare(call.from_user.id, segment, messages, segment_value)
    await state.clear()
    await broadcast_card(call, deps, broadcast_id, deps.config.is_admin(call.from_user.id))
    await call.answer()


def _block_from_item(item: dict) -> ContentBlock:
    source_chat, source_ids = source_of(item)
    return ContentBlock(
        text=item.get("text"),
        media_kind=item.get("media_kind"),
        file_id=item.get("file_id"),
        buttons=item.get("buttons") or [],
        album=item.get("album") or None,
        source_chat=source_chat,
        source_ids=source_ids,
    )


def _eta(total: int, deps) -> str:
    rate = deps.config.messages_per_second or 20
    seconds = int(total / rate) + 1
    if seconds < 60:
        return f"{seconds} сек"
    return f"{seconds // 60} мин"


@router.callback_query(F.data.startswith("a:bc:p:"))
async def cb_preview(call: CallbackQuery, deps) -> None:
    broadcast_id = int(call.data.split(":")[-1])
    for msg in await deps.broadcasts.messages(broadcast_id):
        if "chat_id" in msg and "message_id" in msg:
            await call.bot.copy_message(
                chat_id=call.message.chat.id,
                from_chat_id=msg["chat_id"],
                message_id=msg["message_id"],
            )
        else:
            await send_preview(call, [_block_from_item(msg)])
    await call.answer("Это увидят люди")


@router.callback_query(F.data.startswith("a:bc:x:"))
async def cb_cancel(call: CallbackQuery, deps) -> None:
    """⏹ Остановить: идущая рассылка перестаёт слать со следующего получателя, запланированная — не запустится."""
    broadcast_id = int(call.data.split(":")[-1])
    row = await deps.broadcasts.get(broadcast_id)
    if row is None or row["status"] not in ("queued", "running"):
        await call.answer("Эту рассылку уже нечего останавливать", show_alert=True)
        return
    await deps.broadcasts.cancel(broadcast_id)
    await call.answer("Останавливаю — новым людям больше не отправляю")
    await broadcast_card(call, deps, broadcast_id, deps.config.is_admin(call.from_user.id))


async def _wait_runner(broadcast_id: int, timeout: float = 30.0) -> None:
    """Дождаться, пока остановленный прогон допошлёт текущему получателю и выйдет — иначе его
    последние сообщения могли бы появиться уже после удаления."""
    for task in list(_tasks):
        if task.get_name() == f"broadcast-{broadcast_id}":
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout)
            except Exception:  # noqa: BLE001 — не дождались: удаляем то, что уже в журнале
                pass


@router.callback_query(F.data.startswith("a:bc:rc:"))
async def cb_recall_ask(call: CallbackQuery, deps) -> None:
    broadcast_id = int(call.data.split(":")[-1])
    row = await deps.broadcasts.get(broadcast_id)
    if row is None:
        await broadcast_screen(call, deps)
        await call.answer()
        return
    if row["status"] == "running":
        await call.answer("Рассылка ещё идёт — сначала нажмите «⏹ Остановить»", show_alert=True)
        return
    logged = await deps.sent_log.for_ref("broadcast", broadcast_id)
    people = len({r["user_id"] for r in logged})
    if not logged:
        await show(
            call,
            f"🧯 <b>Рассылка #{broadcast_id}</b>\n\nВ журнале нет отправленных сообщений этой рассылки — удалять "
            "нечего. Так бывает, если она отправлена до появления журнала.",
            kb([[("⬅️ К рассылке", f"a:bc:o:{broadcast_id}")]]),
        )
        await call.answer()
        return
    fresh = [r for r in logged if r["sent_at"] >= int(time.time()) - MAX_AGE_SECONDS]
    old_note = (
        f"\n\nСтарше 48 часов (Telegram не даёт их удалить): {len(logged) - len(fresh)} сообщ."
        if len(fresh) < len(logged)
        else ""
    )
    await show(
        call,
        f"🧯 <b>Удалить рассылку #{broadcast_id} у получателей?</b>\n\n"
        f"Сообщения пропадут из чатов у людей: {people} чел., {len(logged)} сообщ.\n"
        "Вернуть их будет нельзя. Другие рассылки и посты прогрева не затрагиваются."
        f"{old_note}",
        kb([[("🧯 Да, удалить у всех", f"a:bc:rcok:{broadcast_id}")], [("✖️ Нет", f"a:bc:o:{broadcast_id}")]]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("a:bc:rcok:"))
async def cb_recall_do(call: CallbackQuery, deps) -> None:
    broadcast_id = int(call.data.split(":")[-1])
    row = await deps.broadcasts.get(broadcast_id)
    if row is None:
        await broadcast_screen(call, deps)
        await call.answer()
        return
    if row["status"] == "running":
        await call.answer("Рассылка ещё идёт — сначала нажмите «⏹ Остановить»", show_alert=True)
        return
    await call.answer("Удаляю…")
    await show(call, "⏳ Удаляю сообщения у получателей, это может занять несколько минут…")
    await _wait_runner(broadcast_id)
    result = await run_broadcast_recall(call.bot, deps, broadcast_id)
    lines = [f"Удалено у людей: {result['users']} ({result['messages']} сообщ.)"]
    if result["blocked"]:
        lines.append(f"Заблокировали бота: {result['blocked']}")
    if result["too_old"]:
        lines.append(f"Старше 48 часов, удалить нельзя: {result['too_old']} сообщ.")
    if result["failed"]:
        lines.append(f"Не вышло: {result['failed']} — нажмите «Удалить у получателей» ещё раз")
    if not result["messages"] and not result["failed"] and not result["blocked"] and not result["too_old"]:
        lines = ["Удалять было нечего — в журнале нет сообщений этой рассылки."]
    await show(
        call,
        screen_text(f"🧯 Рассылка #{broadcast_id}", "Удаление у получателей выполнено.", "\n".join(lines)),
        kb([[("📨 К рассылке", f"a:bc:o:{broadcast_id}")], [("⬅️ К списку", "a:bc")]]),
    )


def _finished_keyboard(broadcast_id: int, can_recall: bool):
    rows = []
    if can_recall:
        rows.append([("🧯 Удалить у получателей", f"a:bc:rc:{broadcast_id}")])
    rows.append([("📨 Карточка рассылки", f"a:bc:o:{broadcast_id}")])
    return kb(rows)


@router.callback_query(F.data.startswith("a:bc:go:"))
async def cb_go(call: CallbackQuery, deps) -> None:
    broadcast_id = int(call.data.split(":")[-1])
    if not await deps.broadcasts.claim_run(broadcast_id):
        # уже идёт, завершена или остановлена — второй запуск разослал бы всё людям повторно
        await call.answer("Эта рассылка уже запущена или завершена", show_alert=True)
        return
    stop_markup = kb([[("⏹ Остановить", f"a:bc:x:{broadcast_id}")]])
    status_message = await call.message.answer("🚀 Отправляю…", reply_markup=stop_markup)
    await call.answer()

    async def progress(stats: dict[str, int]) -> None:
        try:
            await status_message.edit_text(
                f"🚀 Отправляю…\n\n"
                f"Готово: {stats['sent']} из {stats['total']}\n"
                f"Заблокировали: {stats['blocked']}\n"
                f"Ошибок: {stats['failed']}",
                reply_markup=stop_markup,
            )
        except Exception:  # noqa: BLE001 — Telegram не даёт редактировать слишком часто
            pass

    async def runner() -> None:
        stats = await deps.engine.run(broadcast_id, progress=progress)
        stopped = await deps.broadcasts.status(broadcast_id) in ("cancelled", "recalled")
        title = f"⏹ Рассылка #{broadcast_id} остановлена" if stopped else f"✅ Рассылка #{broadcast_id} завершена"
        text = (
            f"{title}\n\n"
            f"Доставлено: {stats['sent']}\n"
            f"Заблокировали бота: {stats['blocked']}\n"
            f"Ошибок: {stats['failed']}\n"
            f"Всего в базе: {stats['total']}"
        )
        if stopped and stats.get("pending"):
            text += f"\nНе успели отправить: {stats['pending']}"
        markup = _finished_keyboard(broadcast_id, stats["sent"] > 0)
        try:
            await status_message.edit_text(text, reply_markup=markup)
        except Exception:  # noqa: BLE001
            await call.message.answer(text, reply_markup=markup)

    task = asyncio.create_task(runner(), name=f"broadcast-{broadcast_id}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@router.callback_query(F.data.startswith("a:bc:sch:"))
async def cb_schedule(call: CallbackQuery, state: FSMContext) -> None:
    broadcast_id = int(call.data.split(":")[-1])
    await state.set_state(BroadcastNew.waiting_schedule)
    await state.update_data(broadcast_id=broadcast_id)
    now = datetime.now(TZ).strftime("%d.%m %H:%M")
    await show(
        call,
        "🕓 <b>Когда отправить?</b>\n\n"
        f"Время московское, сейчас {now}.\n\n"
        "Форматы: <code>21:30</code>, <code>завтра 10:00</code>, <code>20.09 14:00</code>",
        kb([[("⬅️ Отмена", "a:bc")]]),
    )
    await call.answer()


@router.message(BroadcastNew.waiting_schedule, F.text)
async def on_schedule(message: Message, state: FSMContext, deps) -> None:
    when = parse_schedule(message.text)
    if when is None:
        await message.answer("Не понял время. Примеры: <code>21:30</code>, <code>завтра 10:00</code>, <code>20.09 14:00</code>")
        return
    data = await state.get_data()
    broadcast_id = data["broadcast_id"]
    await deps.db.execute(
        "UPDATE broadcasts SET scheduled_at = ?, status = 'queued' WHERE id = ?",
        (when, broadcast_id),
    )
    await state.clear()
    await broadcast_screen(message, deps)


def parse_schedule(raw: str, now: datetime | None = None) -> int | None:
    """«21:30», «завтра 10:00», «20.09 14:00» → unix-время."""
    text = (raw or "").strip().lower()
    now = now or datetime.now(TZ)

    match = re.match(r"^(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?\s+(\d{1,2}):(\d{2})$", text)
    if match:
        day, month, year, hour, minute = match.groups()
        year_int = int(year) if year else now.year
        if year_int < 100:
            year_int += 2000
        try:
            dt = datetime(year_int, int(month), int(day), int(hour), int(minute), tzinfo=TZ)
        except ValueError:
            return None
        return int(dt.timestamp())

    tomorrow = text.startswith("завтра")
    text = text.replace("завтра", "").replace("сегодня", "").strip()
    match = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if tomorrow or dt <= now:
        dt += timedelta(days=1)
    return int(dt.timestamp())
