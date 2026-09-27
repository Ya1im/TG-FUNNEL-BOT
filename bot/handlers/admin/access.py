"""Доступ клиентов к боту: список, пригласительная ссылка, добавление по ID, выбор роли (stats/admin)."""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.handlers.admin.common import ViewerAdd, kb, screen_text, show

router = Router(name="admin-access")

HINT = "Клиент получает доступ по выбранной роли: «статистика» — только /stats, «полный» — вся админка кроме опасных действий."

ROLE_LABELS = {"stats": "📊 stats", "admin": "🛠 admin"}


def _title(row) -> str:
    name = html.escape(row["name"] or "без имени")
    handle = f" (@{html.escape(row['username'])})" if row["username"] else ""
    return f"{name}{handle}"


def _role_choice_kb(prefix: str, back: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора роли для приглашения/добавления: prefix + ":stats"/":admin"."""
    return kb([
        [("📊 Только статистика", f"{prefix}:stats")],
        [("🛠 Полный доступ", f"{prefix}:admin")],
        [("⬅️ Отмена", back)],
    ])


async def access_screen(target, deps) -> None:
    viewers = await deps.access.list()
    lines = [
        f"{i}. {_title(v)} · <code>{v['tg_id']}</code> · {ROLE_LABELS.get(v['role'], v['role'])}"
        for i, v in enumerate(viewers, start=1)
    ]
    rows = [[("🔗 Пригласительная ссылка", "a:acc:link")], [("➕ Добавить по ID", "a:acc:add")]]
    for v in viewers:
        rows.append([(f"🗑 {(v['name'] or str(v['tg_id']))[:24]}", f"a:acc:del:{v['tg_id']}")])
    rows.append([("⬅️ Назад", "a:set")])
    body = "\n".join(lines) if lines else "Пока никого — создайте пригласительную ссылку."
    await show(target, screen_text("👥 Доступ к админке", HINT, body), kb(rows))


@router.callback_query(F.data == "a:acc")
async def cb_access(call: CallbackQuery, deps, state: FSMContext) -> None:
    await state.clear()
    await access_screen(call, deps)
    await call.answer()


@router.callback_query(F.data == "a:acc:link")
async def cb_link(call: CallbackQuery) -> None:
    await show(
        call,
        screen_text(
            "🔗 Пригласительная ссылка",
            "Какой доступ получит клиент, который перейдёт по ссылке?",
        ),
        _role_choice_kb("a:acc:link", "a:acc"),
    )
    await call.answer()


@router.callback_query(F.data.in_(("a:acc:link:stats", "a:acc:link:admin")))
async def cb_link_role(call: CallbackQuery, deps) -> None:
    role = call.data.rsplit(":", 1)[-1]
    token = await deps.access.create_invite(created_by=call.from_user.id, role=role)
    me = await call.bot.me()
    url = f"https://t.me/{me.username}?start=v_{token}"
    hint = (
        "Одноразовая, действует 7 дней. Отправьте клиенту: он нажмёт «Запустить» и получит "
        + ("/stats." if role == "stats" else "доступ ко всей админке, кроме опасных действий.")
    )
    await show(
        call,
        screen_text(f"🔗 Ссылка для клиента ({ROLE_LABELS[role]})", hint, f"<code>{url}</code>"),
        kb([[("🔗 Новая ссылка", f"a:acc:link:{role}")], [("⬅️ Назад", "a:acc")]]),
    )
    await call.answer()


@router.callback_query(F.data == "a:acc:add")
async def cb_add(call: CallbackQuery) -> None:
    await show(
        call,
        screen_text(
            "➕ Добавить клиента",
            "Какой доступ выдать?",
        ),
        _role_choice_kb("a:acc:add", "a:acc"),
    )
    await call.answer()


@router.callback_query(F.data.in_(("a:acc:add:stats", "a:acc:add:admin")))
async def cb_add_role(call: CallbackQuery, state: FSMContext) -> None:
    role = call.data.rsplit(":", 1)[-1]
    await state.set_state(ViewerAdd.waiting_value)
    await state.update_data(role=role)
    await show(
        call,
        screen_text(
            f"➕ Добавить клиента ({ROLE_LABELS[role]})",
            "Пришлите числовой Telegram ID или перешлите сюда любое сообщение этого человека.",
        ),
        kb([[("⬅️ Отмена", "a:acc")]]),
    )
    await call.answer()


@router.message(ViewerAdd.waiting_value)
async def on_add_value(message: Message, state: FSMContext, deps) -> None:
    tg_id, name, username = None, None, None
    if message.forward_from:
        tg_id = message.forward_from.id
        name, username = message.forward_from.full_name, message.forward_from.username
    elif message.text and message.text.strip().lstrip("-").isdigit():
        tg_id = int(message.text.strip())
        try:
            chat = await message.bot.get_chat(tg_id)
            name = chat.full_name if hasattr(chat, "full_name") else None
            username = chat.username
        except Exception:  # noqa: BLE001 — человек мог ещё не писать боту: добавляем по одному ID
            pass
    if tg_id is None:
        await message.answer(
            "Нужен числовой ID или пересланное сообщение. Если у человека скрыт профиль — "
            "используйте пригласительную ссылку.",
            reply_markup=kb([[("⬅️ Отмена", "a:acc")]]),
        )
        return
    data = await state.get_data()
    role = data.get("role", "stats")
    await deps.access.add(tg_id, name, username, role=role)
    await state.clear()
    await access_screen(message, deps)


@router.callback_query(F.data.startswith("a:acc:del:"))
async def cb_delete_ask(call: CallbackQuery) -> None:
    tg_id = int(call.data.split(":")[-1])
    await show(
        call,
        screen_text("🗑 Убрать доступ?", "Клиент потеряет доступ (статистику или админку — по его роли)."),
        kb([[("🗑 Да, убрать", f"a:acc:delok:{tg_id}")], [("⬅️ Отмена", "a:acc")]]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("a:acc:delok:"))
async def cb_delete_do(call: CallbackQuery, deps) -> None:
    await deps.access.remove(int(call.data.split(":")[-1]))
    await call.answer("Убрал")
    await access_screen(call, deps)
