"""Быстрый приём медиа мимо сценария «Медиатека».

Админ может просто прислать боту файлы (не заходя в 🎬 Медиатека → ➕ Загрузить) — бот
их подхватит, спросит «сохранить всё / выбрать / отменить», а после ответа сам удалит
исходные сообщения и сложит файлы в базу. Чтобы не засорять чат, счётчик собранных файлов
живёт в одном и том же редактируемом сообщении.
"""
from __future__ import annotations

import time

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.content import extract_media
from bot.repo.media import KIND_TITLES
from bot.handlers.admin.common import MediaCapture, MenuRef, kb, show
from bot.handlers.admin.media import media_screen

router = Router(name="admin-capture")

MEDIA_FILTER = (
    F.photo | F.video | F.document | F.audio | F.voice | F.video_note | F.animation | F.sticker
)


def _counts(items: list[dict]) -> str:
    totals: dict[str, int] = {}
    for item in items:
        totals[item["kind"]] = totals.get(item["kind"], 0) + 1
    return ", ".join(f"{KIND_TITLES.get(k, k)}: {n}" for k, n in totals.items())


def _collect_text(items: list[dict]) -> str:
    return (
        "📥 <b>Файлы мимо сценария</b>\n\n"
        f"Поймал: {len(items)} ({_counts(items)})\n\n"
        "Можешь прислать ещё — добавлю в этот же список.\n\n"
        "Сохранить всё в медиатеку или выбрать нужные?"
    )


def _collect_kb():
    return kb(
        [
            [("✅ Сохранить всё", "a:cap:all")],
            [("🔎 Выбрать, что сохранить", "a:cap:pick")],
            [("✖️ Отменить (удалю всё)", "a:cap:cancel")],
        ]
    )


async def _delete_all(bot, items: list[dict]) -> None:
    for item in items:
        try:
            await bot.delete_message(item["chat_id"], item["message_id"])
        except Exception:  # noqa: BLE001 — например, уже удалено
            pass


async def _save_all(deps, items: list[dict]) -> int:
    """Сохраняет файлы в медиатеку с автоименем — слаг руками тут никто не придумывает."""
    stamp = int(time.time())
    for i, item in enumerate(items, start=1):
        slug = f"{item['kind']}_{stamp}_{i}"
        await deps.media.save(
            slug, item["kind"], item["file_id"], item.get("file_unique_id"), item.get("caption")
        )
    return len(items)


@router.message(StateFilter(None), MEDIA_FILTER)
@router.message(MediaCapture.collecting, MEDIA_FILTER)
@router.message(MediaCapture.picking, MEDIA_FILTER)
async def on_stray_media(message: Message, state: FSMContext) -> None:
    found = extract_media(message)
    if not found:
        return
    kind, file_id, file_unique_id = found
    data = await state.get_data()
    items = data.get("items", [])
    items.append(
        {
            "chat_id": message.chat.id,
            "message_id": message.message_id,
            "kind": kind,
            "file_id": file_id,
            "file_unique_id": file_unique_id,
            "caption": message.caption,
        }
    )
    chat_id, message_id = data.get("_menu_chat_id"), data.get("_menu_message_id")
    ref = MenuRef(message.bot, chat_id, message_id) if chat_id and message_id else None
    result = await show(ref or message, _collect_text(items), _collect_kb())

    await state.set_state(MediaCapture.collecting)
    await state.update_data(
        items=items,
        _menu_chat_id=chat_id or (result.chat.id if result else message.chat.id),
        _menu_message_id=message_id or (result.message_id if result else None),
    )


@router.callback_query(MediaCapture.collecting, F.data == "a:cap:all")
async def cb_capture_all(call: CallbackQuery, state: FSMContext, deps) -> None:
    data = await state.get_data()
    items = data.get("items", [])
    saved = await _save_all(deps, items)
    await _delete_all(call.bot, items)
    await state.clear()
    await call.answer(f"Сохранил файлов: {saved}")
    await media_screen(call, deps)


def _pick_text(items: list[dict], keep: set[int]) -> str:
    lines = [
        f"{i + 1}. {KIND_TITLES.get(it['kind'], it['kind'])} — {'сохраню' if i in keep else 'пропущу'}"
        for i, it in enumerate(items)
    ]
    return (
        "🔎 <b>Выбор файлов</b>\n\n"
        "Жми на пункт, чтобы включить/выключить, потом «✅ Сохранить отмеченные».\n\n"
        + "\n".join(lines)
    )


def _pick_kb(items: list[dict], keep: set[int]):
    rows = [
        [
            (
                f"{i + 1}. {'✅' if i in keep else '▫️'} {KIND_TITLES.get(it['kind'], it['kind'])}",
                f"a:cap:tgl:{i}",
            )
        ]
        for i, it in enumerate(items)
    ]
    rows.append([("✅ Сохранить отмеченные", "a:cap:go")])
    rows.append([("✖️ Отменить всё", "a:cap:cancel")])
    return kb(rows)


async def _render_pick(call: CallbackQuery, items: list[dict], keep: set[int]) -> None:
    await show(call, _pick_text(items, keep), _pick_kb(items, keep))


@router.callback_query(MediaCapture.collecting, F.data == "a:cap:pick")
async def cb_capture_pick(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    items = data.get("items", [])
    keep = set(range(len(items)))
    await state.set_state(MediaCapture.picking)
    await state.update_data(keep=list(keep))
    await _render_pick(call, items, keep)
    await call.answer()


@router.callback_query(MediaCapture.picking, F.data.startswith("a:cap:tgl:"))
async def cb_capture_toggle(call: CallbackQuery, state: FSMContext) -> None:
    idx = int(call.data.split(":")[-1])
    data = await state.get_data()
    items = data.get("items", [])
    keep = set(data.get("keep", []))
    keep.symmetric_difference_update({idx})
    await state.update_data(keep=list(keep))
    await _render_pick(call, items, keep)
    await call.answer()


@router.callback_query(MediaCapture.picking, F.data == "a:cap:go")
async def cb_capture_go(call: CallbackQuery, state: FSMContext, deps) -> None:
    data = await state.get_data()
    items = data.get("items", [])
    keep = set(data.get("keep", []))
    chosen = [it for i, it in enumerate(items) if i in keep]
    skipped = [it for i, it in enumerate(items) if i not in keep]
    saved = await _save_all(deps, chosen)
    await _delete_all(call.bot, chosen)
    await _delete_all(call.bot, skipped)
    await state.clear()
    await call.answer(f"Сохранил: {saved}")
    await media_screen(call, deps)


@router.callback_query(F.data == "a:cap:cancel")
async def cb_capture_cancel(call: CallbackQuery, state: FSMContext, deps) -> None:
    data = await state.get_data()
    items = data.get("items", [])
    await _delete_all(call.bot, items)
    await state.clear()
    await call.answer("Отменил, файлы удалил")
    await media_screen(call, deps)
