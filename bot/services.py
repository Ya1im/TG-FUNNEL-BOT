"""Сценарии пользователя: старт, проверка подписки, выдача материала."""
from __future__ import annotations

import logging

from aiogram.exceptions import TelegramAPIError

from bot.content import ContentBlock, apply_placeholders
from bot.keyboards import link_kb, subscribe_kb
from bot.repo.funnel import block_from_row
from bot.sender import safe_send, send_block

log = logging.getLogger(__name__)


async def send_welcome(bot, deps, chat_id: int) -> None:
    """Кружок (или другое медиа) + меню с кнопками подписки."""
    media_id = await deps.settings.get_int("welcome_note_media_id")
    if media_id:
        row = await deps.media.get(media_id)
        if row:
            block = ContentBlock(media_kind=row["kind"], file_id=row["file_id"])
            await send_block(block, bot, chat_id, users=deps.users, limiter=deps.limiter)

    text = apply_placeholders(
        await deps.settings.get("menu_text"), await deps.settings.get("channel_url")
    )
    kb = await subscribe_kb(deps.settings)

    async def action():
        return await bot.send_message(chat_id, text, reply_markup=kb)

    await safe_send(action, chat_id=chat_id, users=deps.users, limiter=deps.limiter)


async def start_flow(bot, deps, tg_user, chat_id: int, payload: str | None = None) -> None:
    await deps.users.upsert(
        tg_user.id,
        getattr(tg_user, "username", None),
        getattr(tg_user, "first_name", None),
        source=(payload or None),
    )
    user = await deps.users.get(tg_user.id)
    if user["material_sent_at"]:
        text = await deps.settings.get("already_started_text")

        async def action():
            return await bot.send_message(chat_id, text)

        await safe_send(action, chat_id=chat_id, users=deps.users, limiter=deps.limiter)
        return
    await send_welcome(bot, deps, chat_id)


async def deliver_material(bot, deps, chat_id: int) -> None:
    """Материал + персональная ссылка в закрытый канал + запуск прогрева."""
    user = await deps.users.get(chat_id)

    intro = (await deps.settings.get("material_intro")).strip()
    if intro:
        await send_block(
            ContentBlock(text=intro), bot, chat_id, user=user, users=deps.users, limiter=deps.limiter
        )

    for row in await deps.material.list_blocks(only_enabled=True):
        block = block_from_row(row)
        if block.is_empty:
            continue
        await send_block(block, bot, chat_id, user=user, users=deps.users, limiter=deps.limiter)

    invite = await build_invite_link(bot, deps, chat_id)
    if invite:
        text = await deps.settings.get("private_text")
        kb = link_kb(await deps.settings.get("btn_private"), invite)

        async def action():
            return await bot.send_message(chat_id, text, reply_markup=kb)

        await safe_send(action, chat_id=chat_id, users=deps.users, limiter=deps.limiter)

    await deps.users.mark_material_sent(chat_id)
    await deps.funnel.enqueue(chat_id)


async def build_invite_link(bot, deps, user_id: int) -> str | None:
    """Персональная одноразовая ссылка; если не вышло — общая из настроек."""
    fallback = (await deps.settings.get("private_invite_link")).strip() or None
    private_id = (await deps.settings.get("private_channel_id")).strip()
    if not private_id:
        return fallback
    chat_id = int(private_id) if private_id.lstrip("-").isdigit() else private_id
    try:
        link = await bot.create_chat_invite_link(
            chat_id=chat_id, name=f"user {user_id}", member_limit=1
        )
        return link.invite_link
    except TelegramAPIError as exc:
        log.warning("Не смог создать invite-ссылку: %s", exc)
        return fallback


async def check_subscription_flow(bot, deps, user_id: int, chat_id: int) -> bool:
    """Возвращает True, если подписка есть (и материал выдан)."""
    if not (await deps.settings.get("channel_id")).strip():
        log.warning("Канал не задан — проверка подписки пропускает всех")
    if await deps.gate.check(user_id):
        user = await deps.users.get(user_id)
        if not (user and user["material_sent_at"]):
            await deliver_material(bot, deps, chat_id)
        return True
    return False
