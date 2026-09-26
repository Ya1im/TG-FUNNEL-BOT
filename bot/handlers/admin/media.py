"""Медиатека: загрузка файлов и хранение их file_id."""
from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.content import ContentBlock, extract_media
from bot.repo.media import KIND_TITLES
from bot.sender import send_block
from bot.handlers.admin.common import KIND_ICONS, MediaAdd, kb, screen_text, show

router = Router(name="admin-media")

HINT = "Файлы для приветствия, материала и прогрева. Можно просто прислать файл боту — он предложит сохранить."


async def media_screen(target, deps) -> None:
    items = await deps.media.list(limit=20)
    rows = [[("➕ Загрузить файл", "a:media:add")]]
    lines = []
    for row in items:
        icon = KIND_ICONS.get(row["kind"], "📎")
        lines.append(f"{icon} <code>{row['slug']}</code>")
        rows.append([(f"{icon} {row['slug']}", f"a:media:s:{row['id']}")])
    rows.append([("⬅️ Назад", "a:set")])
    total = await deps.media.count()
    body = "\n".join(lines) if lines else "Пока пусто."
    await show(target, screen_text(f"🎬 Медиатека · {total}", HINT, body), kb(rows))


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
        "➕ <b>Загрузка файла</b>\n\n"
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
        f"Принял: {KIND_TITLES.get(kind, kind)}.\n\n"
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
    await media_screen(message, deps)


async def media_card(target, deps, media_id: int) -> None:
    """Карточка файла — правим то же сообщение, живое превью шлём только по кнопке «Показать»."""
    row = await deps.media.get(media_id)
    if not row:
        await show(target, "Файл не найден.", kb([[("⬅️ К медиатеке", "a:media")]]))
        return
    title = KIND_TITLES.get(row["kind"], row["kind"])
    text = (
        f"🎬 <b>{title}</b>\n\n"
        f"Имя: <code>{row['slug']}</code>\n"
        f"Подпись: {row['caption'] or '<i>нет</i>'}"
    )
    await show(
        target,
        text,
        kb(
            [
                [("👁 Показать", f"a:media:prev:{media_id}")],
                [("🗑 Удалить", f"a:media:del:{media_id}")],
                [("⬅️ К медиатеке", "a:media")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("a:media:s:"))
async def cb_media_show(call: CallbackQuery, deps) -> None:
    media_id = int(call.data.split(":")[-1])
    await media_card(call, deps, media_id)
    await call.answer()


@router.callback_query(F.data.startswith("a:media:prev:"))
async def cb_media_preview(call: CallbackQuery, deps) -> None:
    """Живое превью — по кнопке, отдельным сообщением, и без падения на битом file_id."""
    media_id = int(call.data.split(":")[-1])
    row = await deps.media.get(media_id)
    if not row:
        await call.answer("Файл не найден", show_alert=True)
        return
    block = ContentBlock(text=row["caption"], media_kind=row["kind"], file_id=row["file_id"])
    outcome = await send_block(block, call.bot, call.message.chat.id)
    if not outcome.ok:
        await call.answer(
            "Не смог отправить файл — похоже, он от другого бота (сменился токен). "
            "Удали и залей заново.",
            show_alert=True,
        )
        return
    await call.answer()


@router.callback_query(F.data.startswith("a:media:del:"))
async def cb_media_delete(call: CallbackQuery, deps) -> None:
    media_id = int(call.data.split(":")[-1])
    await deps.media.delete(media_id)
    await call.answer("Удалил")
    await media_screen(call, deps)
