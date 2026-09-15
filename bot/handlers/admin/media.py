"""Медиатека: загрузка файлов и хранение их file_id."""
from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.content import ContentBlock, extract_media
from bot.repo.media import KIND_TITLES
from bot.sender import send_block
from bot.handlers.admin.common import MediaAdd, kb, show

router = Router(name="admin-media")

HINT = (
    "🎬 <b>Медиатека</b>\n\n"
    "Сюда складываются файлы, из которых собираются приветствие, материал и прогрев.\n"
    "Загружается любой тип: фото, видео, кружок, документ, аудио, голосовое, гифка."
)


async def media_screen(target, deps) -> None:
    items = await deps.media.list(limit=20)
    rows = [[("➕ Загрузить файл", "a:media:add")]]
    lines = []
    for row in items:
        title = KIND_TITLES.get(row["kind"], row["kind"])
        lines.append(f"• <code>{row['slug']}</code> — {title}")
        rows.append([(f"{row['slug']} ({title})", f"a:media:s:{row['id']}")])
    rows.append([("⬅️ Назад", "a:menu")])
    total = await deps.media.count()
    text = HINT + f"\n\nВсего файлов: {total}\n" + ("\n".join(lines) if lines else "\nПока пусто.")
    await show(target, text, kb(rows))


@router.callback_query(F.data == "a:media")
async def cb_media(call: CallbackQuery, deps, state: FSMContext) -> None:
    await state.clear()
    await media_screen(call, deps)
    await call.answer()


@router.callback_query(F.data == "a:media:add")
async def cb_media_add(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(MediaAdd.waiting_file)
    await show(
        call,
        "Пришли файл одним сообщением: фото, видео, кружок, документ, аудио, голосовое или гифку.",
        kb([[("⬅️ Отмена", "a:media")]]),
    )
    await call.answer()


@router.message(MediaAdd.waiting_file)
async def on_media_file(message: Message, state: FSMContext) -> None:
    found = extract_media(message)
    if not found:
        await message.answer("Это не файл. Пришли фото, видео, кружок, документ, аудио или гифку.")
        return
    kind, file_id, file_unique_id = found
    await state.update_data(kind=kind, file_id=file_id, file_unique_id=file_unique_id)
    await state.set_state(MediaAdd.waiting_slug)
    await message.answer(
        f"Принял: {KIND_TITLES.get(kind, kind)}.\n"
        "Как назвать? Коротким именем латиницей, например <code>krug_privet</code> — "
        "по нему будешь выбирать файл в прогреве и материале."
    )


@router.message(MediaAdd.waiting_slug, F.text)
async def on_media_slug(message: Message, state: FSMContext, deps) -> None:
    slug = re.sub(r"[^a-zA-Z0-9_\-]+", "_", message.text.strip().lower())[:40].strip("_")
    if not slug:
        await message.answer("Имя пустое. Напиши что-то вроде <code>krug_privet</code>.")
        return
    data = await state.get_data()
    await deps.media.save(slug, data["kind"], data["file_id"], data.get("file_unique_id"))
    await state.clear()
    await message.answer(f"Сохранил как <code>{slug}</code> ✅")
    await media_screen(message, deps)


@router.callback_query(F.data.startswith("a:media:s:"))
async def cb_media_show(call: CallbackQuery, deps) -> None:
    media_id = int(call.data.split(":")[-1])
    row = await deps.media.get(media_id)
    if not row:
        await call.answer("Файл не найден", show_alert=True)
        return
    await send_block(
        ContentBlock(media_kind=row["kind"], file_id=row["file_id"]),
        call.bot,
        call.message.chat.id,
    )
    await call.message.answer(
        f"<code>{row['slug']}</code> — {KIND_TITLES.get(row['kind'], row['kind'])}",
        reply_markup=kb([[("🗑 Удалить", f"a:media:del:{media_id}")], [("⬅️ К медиатеке", "a:media")]]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("a:media:del:"))
async def cb_media_delete(call: CallbackQuery, deps) -> None:
    media_id = int(call.data.split(":")[-1])
    await deps.media.delete(media_id)
    await call.answer("Удалил")
    await media_screen(call, deps)
