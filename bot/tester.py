"""Панель тестового прогона: кто тестировщик и как выглядит ход воронки у него."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from bot.preprod import human_span, is_enabled, tester_ids
from bot.repo.funnel import FAST_STEP_SECONDS, NOT_SCHEDULED

MSK = timezone(timedelta(hours=3))


async def is_tester(deps, user_id: int) -> bool:
    return user_id in await tester_ids(deps.settings, deps.config.admin_ids, deps.db)


def _msk(ts: int) -> str:
    return f"{datetime.fromtimestamp(int(ts), MSK):%d.%m %H:%M} МСК"


async def panel_text(deps, user_id: int, now: int | None = None) -> str:
    now = int(now if now is not None else time.time())
    user = await deps.users.get(user_id)
    fast = bool(user and user["funnel_fast"])
    rows = list(await deps.funnel.user_progress(user_id))
    preprod = await is_enabled(deps.settings)
    lines = [
        "🧪 <b>Тестовый прогон</b>",
        "",
        f"Скорость: {f'⚡ ускоренно ({FAST_STEP_SECONDS} с между постами)' if fast else '🐢 реальное время'}",
        "Предпрод: " + (
            "🟡 включён — публикации идут только тестировщикам, перед постом приходит пометка «пришло бы через…»"
            if preprod else "🟢 выключен — идёт продакшен, пометок нет"
        ),
    ]
    if user is None or not user["material_sent_at"]:
        lines += ["", "Урок вам ещё не выдан. «🔄 Начать заново» — пройти всё с приветствия, "
                      "«⏭ Сразу к прогреву» — получить урок без подписки."]
    sent = sum(1 for r in rows if r["status"] == "sent")
    lines += ["", f"Прогресс: {sent} из {len(rows)} постов", ""]
    head_marked = False
    for index, r in enumerate(rows, start=1):
        delay = human_span(r["delay_seconds"])
        status = r["status"]
        if status == "sent":
            lines.append(f"✅ {index} · {delay} · отправлен {_msk(r['sent_at'])}")
        elif status == "skipped":
            lines.append(f"⏭ {index} · пропущен ({r['last_error'] or 'пропущен'})")
        elif status == "failed":
            lines.append(f"❌ {index} · не отправился ({r['last_error'] or 'ошибка'})")
        elif status == "pending" and r["due_at"] < NOT_SCHEDULED and not head_marked:
            head_marked = True
            left = max(0, int(r["due_at"]) - now)
            lines.append(f"⏳ {index} · {delay} · ближайший: {_msk(r['due_at'])} (через {human_span(left)})")
        else:
            after = "после выдачи урока" if index == 1 else "после предыдущего поста"
            lines.append(f"· {index} · через {delay} {after}")
    if not rows:
        lines.append("В воронке пока нет включённых шагов.")
    return "\n".join(lines)
