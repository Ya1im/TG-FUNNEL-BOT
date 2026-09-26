"""Доступ «только статистика»: список клиентов, пригласительная ссылка, добавление по ID."""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.handlers.admin.common import ViewerAdd, kb, screen_text, show

router = Router(name="admin-access")

HINT = "Клиент получает одну команду — /stats. Админка и рассылки ему недоступны."


def _title(row) -> str:
    name = html.escape(row["name"] or "без имени")
    handle = f" (@{html.escape(row['username'])})" if row["username"] else ""
    return f"{name}{handle}"


async def access_screen(target, deps) -> None:
    viewers = await deps.viewers.list()
    lines = [f"{i}. {_title(v)} · <code>{v['tg_id']}</code>" for i, v in enumerate(viewers, start=1)]
    rows = [[("🔗 Пригласительная ссылка", "a:acc:link")], [("➕ Добавить по ID", "a:acc:add")]]
    for v in viewers:
        rows.append([(f"🗑 {(v['name'] or str(v['tg_id']))[:24]}", f"a:acc:del:{v['tg_id']}")])
    rows.append([("⬅️ Назад", "a:set")])
    body = "\n".join(lines) if lines else "Пока никого — создайте пригласительную ссылку."
    await show(target, screen_text("👥 Доступ к статистике", HINT, body), kb(rows))


@router.callback_query(F.data == "a:acc")
async def cb_access(call: CallbackQuery, deps, state: FSMContext) -> None:
    await state.clear()
    await access_screen(call, deps)
    await call.answer()


@router.callback_query(F.data == "a:acc:link")
async def cb_link(call: CallbackQuery, deps) -> None:
    token = await deps.viewers.create_invite(created_by=call.from_user.id)
    me = await call.bot.me()
    url = f"https://t.me/{me.username}?start=v_{token}"
    await show(
        call,
        screen_text(
            "🔗 Ссылка для клиента",
            "Одноразовая, действует 7 дней. Отправьте клиенту: он нажмёт «Запустить» и получит /stats.",
            f"<code>{url}</code>",
        ),
        kb([[("🔗 Новая ссылка", "a:acc:link")], [("⬅️ Назад", "a:acc")]]),
    )
    await call.answer()


@router.callback_query(F.data == "a:acc:add")
async def cb_add(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ViewerAdd.waiting_value)
    await show(
        call,
        screen_text(
            "➕ Добавить клиента",
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
    await deps.viewers.add(tg_id, name, username)
    await state.clear()
    await access_screen(message, deps)


@router.callback_query(F.data.startswith("a:acc:del:"))
async def cb_delete_ask(call: CallbackQuery) -> None:
    tg_id = int(call.data.split(":")[-1])
    await show(
        call,
        screen_text("🗑 Убрать доступ?", "Клиент перестанет получать статистику по команде /stats."),
        kb([[("🗑 Да, убрать", f"a:acc:delok:{tg_id}")], [("⬅️ Отмена", "a:acc")]]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("a:acc:delok:"))
async def cb_delete_do(call: CallbackQuery, deps) -> None:
    await deps.viewers.remove(int(call.data.split(":")[-1]))
    await call.answer("Убрал")
    await access_screen(call, deps)
