"""Сценарии пользователя: старт, проверка подписки, выдача материала."""
from __future__ import annotations

import logging
import time

from aiogram.exceptions import TelegramAPIError

from bot.preprod import allowed_user_ids
from bot.content import ContentBlock, apply_placeholders
from bot.keyboards import link_kb, subscribe_kb
from bot.repo.funnel import block_from_row
from bot.sender import BLOCKED, safe_send, send_block

log = logging.getLogger(__name__)


async def send_menu(bot, deps, chat_id: int) -> None:
    """Текст меню с кнопками подписки — без кружка (для повторных /start)."""
    text = apply_placeholders(
        await deps.settings.get("menu_text"), await deps.settings.get("channel_url")
    )
    kb = await subscribe_kb(deps.settings)

    async def action():
        return await bot.send_message(chat_id, text, reply_markup=kb)

    await safe_send(action, chat_id=chat_id, users=deps.users, limiter=deps.limiter)


async def send_welcome(bot, deps, chat_id: int) -> None:
    """Кружок (или другое медиа) + меню с кнопками подписки — для самого первого захода."""
    media_id = await deps.settings.get_int("welcome_note_media_id")
    if media_id:
        row = await deps.media.get(media_id)
        if row:
            block = ContentBlock(media_kind=row["kind"], file_id=row["file_id"])
            await send_block(block, bot, chat_id, users=deps.users, limiter=deps.limiter)
    await send_menu(bot, deps, chat_id)


async def send_repeat_start(bot, deps, chat_id: int, user=None) -> None:
    """Ответ на повторный /start — произвольные блоки, которые собрал админ.

    Раньше это был один фиксированный текст (already_started_text). Теперь —
    как материал: сколько угодно сообщений любого формата (текст/медиа/кнопки)
    по порядку. Если админ не добавил ни одного блока — ничего не шлём."""
    for row in await deps.repeat_start.list_blocks(only_enabled=True):
        block = block_from_row(row)
        if block.is_empty:
            continue
        await send_block(block, bot, chat_id, user=user, users=deps.users, limiter=deps.limiter)


async def migrate_repeat_start_blocks(deps) -> None:
    """Одноразовая миграция: старый already_started_text → первый блок.

    Выполняется один раз за всю жизнь базы (флаг в settings), а не «пока блоков
    нет» — иначе если админ сам удалит все блоки, желая вообще ничего не слать
    на повторный /start, эта функция при следующем запуске бота вернула бы
    старый текст обратно."""
    if (await deps.settings.get("repeat_start_migrated")).strip():
        return
    text = (await deps.settings.get("already_started_text")).strip()
    if text:
        await deps.repeat_start.add_block(text=text)
    await deps.settings.set("repeat_start_migrated", "1")


async def start_flow(bot, deps, tg_user, chat_id: int, payload: str | None = None) -> None:
    is_new = await deps.users.upsert(
        tg_user.id,
        getattr(tg_user, "username", None),
        getattr(tg_user, "first_name", None),
        source=(payload or None),
    )
    user = await deps.users.get(tg_user.id)
    if user["material_sent_at"]:
        await send_repeat_start(bot, deps, chat_id, user=user)
        return
    if is_new:
        await send_welcome(bot, deps, chat_id)
    else:
        # Повторный /start до получения материала — кружок уже видел, шлём только меню
        await send_menu(bot, deps, chat_id)


async def send_private_invite(bot, deps, chat_id: int) -> bool:
    """Личная ссылка в закрытый канал (награда за подписку). True — ссылка ушла."""
    invite = await build_invite_link(bot, deps, chat_id)
    if not invite:
        return False
    text = await deps.settings.get("private_text")
    kb = link_kb(await deps.settings.get("btn_private"), invite)

    async def action():
        return await bot.send_message(chat_id, text, reply_markup=kb)

    await safe_send(action, chat_id=chat_id, users=deps.users, limiter=deps.limiter)
    await deps.users.mark_invite_sent(chat_id)
    return True


async def deliver_material(bot, deps, chat_id: int, send_invite: bool = True) -> bool:
    """Материал + персональная ссылка в закрытый канал + запуск прогрева.

    send_invite=False — без личной ссылки в закрытый канал (автовыдача неподписчику).
    False — человек заблокировал бота, урок не выдан и воронка не запущена."""
    user = await deps.users.get(chat_id)

    intro = (await deps.settings.get("material_intro")).strip()
    if intro:
        outcome = await send_block(
            ContentBlock(text=intro), bot, chat_id, user=user, users=deps.users, limiter=deps.limiter
        )
        if outcome.status == BLOCKED:
            return False

    for row in await deps.material.list_blocks(only_enabled=True):
        block = block_from_row(row)
        if block.is_empty:
            continue
        outcome = await send_block(block, bot, chat_id, user=user, users=deps.users, limiter=deps.limiter)
        if outcome.status == BLOCKED:
            return False

    if send_invite:
        await send_private_invite(bot, deps, chat_id)

    await deps.users.mark_material_sent(chat_id)
    await deps.funnel.enqueue(chat_id)
    return True


async def deliver_material_once(
    bot, deps, chat_id: int, *, send_invite: bool = True, now: int | None = None
) -> bool:
    """Выдать урок ровно один раз: подписка и автовыдача идут через этот вход.

    False — урок уже выдан или его прямо сейчас выдаёт другой вызов."""
    now = int(now if now is not None else time.time())
    if not await deps.users.claim_delivery(chat_id, now):
        return False
    try:
        delivered = await deliver_material(bot, deps, chat_id, send_invite=send_invite)
    except Exception:
        await deps.users.release_claim(chat_id)
        raise
    if not delivered:
        await deps.users.release_claim(chat_id)
    return delivered


async def init_auto_delivery(deps, now: int | None = None) -> None:
    """Один раз при первом запуске новой версии: запоминаем «с какого момента» автовыдача действует,
    чтобы она не накрыла разом всех, кто нажал /start до её появления."""
    if (await deps.settings.get("auto_deliver_since")).strip():
        return
    await deps.settings.set("auto_deliver_since", str(int(now if now is not None else time.time())))


async def auto_deliver_due(bot, deps, now: int | None = None) -> int:
    """Раз в тик: выдать урок тем, кто не подписался/не нажал «Проверить» за отведённое время."""
    minutes = await deps.settings.get_int("auto_deliver_minutes")
    if not minutes or minutes <= 0:
        return 0
    now = int(now if now is not None else time.time())
    delivered = 0
    since = await deps.settings.get_int("auto_deliver_since") or 0
    allowed = await allowed_user_ids(
        deps.settings, deps.config.admin_ids if getattr(deps, "config", None) else (), deps.db
    )
    for user_id in await deps.users.due_for_auto_delivery(now, minutes, since=since, only_users=allowed):
        try:
            state = await deps.gate.status(user_id, cached_seconds=300)
            if await deliver_material_once(bot, deps, user_id, send_invite=(state == "yes"), now=now):
                delivered += 1
        except Exception:  # noqa: BLE001 — один сбойный пользователь не должен останавливать остальных
            log.exception("Автовыдача урока пользователю %s не удалась", user_id)
    if delivered:
        log.info("Автовыдача урока: %s", delivered)
    return delivered


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
            await deliver_material_once(bot, deps, chat_id)
        elif not user["invite_sent_at"]:
            # урок выдан автоматически (без личной ссылки), а человек подписался — ссылка положена ему
            await send_private_invite(bot, deps, chat_id)
        return True
    return False
