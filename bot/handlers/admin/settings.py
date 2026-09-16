"""Настройки: канал, закрытый канал, кружок приветствия, тексты."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.repo.media import KIND_TITLES
from bot.handlers.admin.common import ChannelSet, SettingEdit, kb, message_text, preview, show

router = Router(name="admin-settings")

TEXT_KEYS = {
    "menu_text": "Текст меню (после кружка)",
    "btn_subscribe": "Кнопка «Подписаться»",
    "btn_check": "Кнопка «Я подписан»",
    "not_subscribed_alert": "Всплывашка «подписки нет»",
    "subscribed_ok_alert": "Всплывашка «подписка есть»",
    "material_intro": "Подводка перед материалом",
    "private_text": "Текст к ссылке в закрытый канал",
    "btn_private": "Кнопка «Войти в закрытый канал»",
    "reminder_text": "Напоминание подписаться (в прогреве)",
    "already_started_text": "Ответ на повторный /start",
}


async def settings_screen(target, deps) -> None:
    data = await deps.settings.all()
    note_id = data.get("welcome_note_media_id") or ""
    note = "не задано"
    if note_id.isdigit():
        row = await deps.media.get(int(note_id))
        if row:
            note = f"{row['slug']} ({KIND_TITLES.get(row['kind'], row['kind'])})"
    title = f" — {data['channel_title']}" if data.get("channel_title") else ""
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        "Каналы, приветствие и все тексты, которые бот показывает пользователям.\n\n"
        f"📢 Канал для проверки: <code>{data.get('channel_id') or 'не задан'}</code>{title}\n"
        f"🔗 Ссылка на канал: {data.get('channel_url') or 'не задана'}\n"
        f"🔒 Закрытый канал: <code>{data.get('private_channel_id') or 'не задан'}</code>\n\n"
        f"👋 Приветствие: {note}"
    )
    await show(
        target,
        text,
        kb(
            [
                [("📢 Канал для проверки", "a:set:channel")],
                [("🔒 Закрытый канал", "a:set:private")],
                [("👋 Приветствие", "a:set:note")],
                [("✏️ Тексты и кнопки", "a:set:texts")],
                [("⬅️ Назад", "a:menu")],
            ]
        ),
    )


@router.callback_query(F.data == "a:set")
async def cb_settings(call: CallbackQuery, deps, state: FSMContext) -> None:
    await state.clear()
    await settings_screen(call, deps)
    await call.answer()


# --- каналы ---------------------------------------------------------------


@router.callback_query(F.data.in_({"a:set:channel", "a:set:private"}))
async def cb_channel(call: CallbackQuery, state: FSMContext) -> None:
    field = "private" if call.data.endswith("private") else "channel"
    await state.set_state(ChannelSet.waiting_value)
    await state.update_data(field=field)
    is_private = field == "private"
    title = "🔒 Закрытый канал" if is_private else "📢 Канал для проверки"
    what = "закрытый канал (куда выдаём доступ)" if is_private else "канал для проверки подписки"
    await show(
        call,
        f"{title}\n\n"
        f"Пришли {what}: <code>@username</code>, числовой ID вида <code>-100…</code> "
        "или просто перешли сюда любой пост из этого канала.\n\n"
        "⚠️ Бот должен быть администратором канала — иначе Telegram не даст проверять подписку.\n\n"
        "Чтобы очистить — отправь <code>-</code>",
        kb([[("⬅️ Отмена", "a:set")]]),
    )
    await call.answer()


@router.message(ChannelSet.waiting_value)
async def on_channel_value(message: Message, state: FSMContext, deps) -> None:
    data = await state.get_data()
    field = data.get("field", "channel")
    prefix = "private_channel_id" if field == "private" else "channel_id"

    raw: str | int | None = None
    if message.forward_from_chat:
        raw = message.forward_from_chat.id
    elif message.text:
        raw = message.text.strip()

    if raw in ("-", "", None):
        await deps.settings.set(prefix, "")
        if field == "channel":
            await deps.settings.set("channel_url", "")
            await deps.settings.set("channel_title", "")
        await state.clear()
        await settings_screen(message, deps)
        return

    if isinstance(raw, str) and not raw.lstrip("-").isdigit() and not raw.startswith("@"):
        raw = f"@{raw.split('/')[-1]}"
    chat_id = int(raw) if str(raw).lstrip("-").isdigit() else raw

    try:
        chat = await message.bot.get_chat(chat_id)
        me = await message.bot.me()
        member = await message.bot.get_chat_member(chat.id, me.id)
    except TelegramAPIError as exc:
        await message.answer(
            f"Не получилось открыть канал: {exc}\n\n"
            "Проверь, что бот добавлен в канал администратором, и пришли ещё раз."
        )
        return

    chat_type = str(getattr(chat.type, "value", chat.type))
    if chat_type not in ("channel", "supergroup", "group") or chat.id == me.id:
        await message.answer(
            "Это не канал.\n\n"
            f"Telegram вернул: <b>{chat.title or chat.full_name}</b> "
            f"(тип <code>{chat_type}</code>, id <code>{chat.id}</code>)\n\n"
            "Пришли <code>@username</code> своего канала. Если канал приватный и username у него нет — "
            "перешли сюда любой пост из канала (в настройках канала должно быть разрешено "
            "показывать отправителя) либо пришли числовой id вида <code>-100…</code>.",
            reply_markup=kb([[("⬅️ Отмена", "a:set")]]),
        )
        return

    status = str(getattr(member.status, "value", member.status))
    if status not in ("administrator", "creator"):
        await state.update_data(
            pending_id=str(chat.id),
            pending_title=chat.title or "",
            pending_url=f"https://t.me/{chat.username}" if chat.username else (chat.invite_link or ""),
        )
        await message.answer(
            "Не вижу у бота прав администратора в этом канале.\n\n"
            f"Проверял: бот <b>@{me.username}</b> (id <code>{me.id}</code>)\n"
            f"Канал: <b>{chat.title}</b> (id <code>{chat.id}</code>)\n"
            f"Статус бота там: <code>{status}</code>\n\n"
            "Обычно это значит, что админом сделали другого бота или права дали в другом канале.\n"
            "Проверь в канале: Управление → Администраторы — там должен быть именно "
            f"@{me.username}.\n\n"
            "Если уверен, что всё верно — сохрани как есть, проверку подписки потом протестируем.",
            reply_markup=kb([[("💾 Сохранить всё равно", "a:set:force")], [("⬅️ Отмена", "a:set")]]),
        )
        return

    await deps.settings.set(prefix, str(chat.id))
    if field == "channel":
        url = f"https://t.me/{chat.username}" if chat.username else (chat.invite_link or "")
        await deps.settings.set("channel_url", url)
        await deps.settings.set("channel_title", chat.title or "")
    await state.clear()
    await settings_screen(message, deps)


@router.callback_query(F.data == "a:set:force")
async def cb_channel_force(call: CallbackQuery, state: FSMContext, deps) -> None:
    """Сохранить канал, даже если Telegram не показал у бота прав админа."""
    data = await state.get_data()
    if not data.get("pending_id"):
        await call.answer("Нечего сохранять, начни заново", show_alert=True)
        return
    field = data.get("field", "channel")
    prefix = "private_channel_id" if field == "private" else "channel_id"
    await deps.settings.set(prefix, data["pending_id"])
    if field == "channel":
        await deps.settings.set("channel_url", data.get("pending_url", ""))
        await deps.settings.set("channel_title", data.get("pending_title", ""))
    await state.clear()
    await call.answer("Сохранил")
    await settings_screen(call, deps)


# --- кружок приветствия ---------------------------------------------------


@router.callback_query(F.data == "a:set:note")
async def cb_note(call: CallbackQuery, deps) -> None:
    rows = [[("🚫 Без приветственного медиа", "a:set:note:0")]]
    for row in await deps.media.list(limit=20):
        rows.append(
            [(f"{row['slug']} ({KIND_TITLES.get(row['kind'], row['kind'])})", f"a:set:note:{row['id']}")]
        )
    rows.append([("⬅️ Назад", "a:set")])
    await show(
        call,
        "👋 <b>Приветствие</b>\n\n"
        "Это первое, что видит человек после /start — обычно кружок, но подойдёт любое медиа.\n\n"
        "Нет нужного файла в списке — сначала залей его в 🎬 Медиатеку.",
        kb(rows),
    )
    await call.answer()


@router.callback_query(F.data.startswith("a:set:note:"))
async def cb_note_set(call: CallbackQuery, deps) -> None:
    media_id = int(call.data.split(":")[-1])
    await deps.settings.set("welcome_note_media_id", "" if media_id == 0 else str(media_id))
    await call.answer("Сохранил")
    await settings_screen(call, deps)


# --- тексты ---------------------------------------------------------------


@router.callback_query(F.data == "a:set:texts")
async def cb_texts(call: CallbackQuery, deps) -> None:
    data = await deps.settings.all()
    rows = [[(label, f"a:set:t:{key}")] for key, label in TEXT_KEYS.items()]
    rows.append([("⬅️ Назад", "a:set")])
    lines = [f"• <b>{label}</b>: {preview(data.get(key), 40)}" for key, label in TEXT_KEYS.items()]
    await show(
        call,
        "✏️ <b>Тексты и кнопки</b>\n\n"
        "Все подписи и сообщения, которые бот шлёт пользователям в сценарии. "
        "Жми на пункт, чтобы поменять.\n\n" + "\n".join(lines),
        kb(rows),
    )
    await call.answer()


@router.callback_query(F.data.startswith("a:set:t:"))
async def cb_text_edit(call: CallbackQuery, deps, state: FSMContext) -> None:
    key = call.data.split(":")[-1]
    if key not in TEXT_KEYS:
        await call.answer("Неизвестная настройка", show_alert=True)
        return
    current = await deps.settings.get(key)
    await state.set_state(SettingEdit.waiting_value)
    await state.update_data(key=key)
    await show(
        call,
        f"✏️ <b>{TEXT_KEYS[key]}</b>\n\n"
        f"Сейчас:\n{current or '<i>пусто</i>'}\n\n"
        "Пришли новый текст.\n\n"
        "Подсказка: <code>{name}</code> в тексте подставится именем пользователя.",
        kb([[("⬅️ Отмена", "a:set:texts")]]),
    )
    await call.answer()


@router.message(SettingEdit.waiting_value)
async def on_text_value(message: Message, state: FSMContext, deps) -> None:
    text = message_text(message)
    if not text:
        await message.answer("Нужен текст.")
        return
    data = await state.get_data()
    await deps.settings.set(data["key"], text)
    await state.clear()
    await settings_screen(message, deps)
