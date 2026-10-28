"""Этапы воронки: кто ещё до урока, кто в прогреве (и на каком посте), кто закончил — для рассылок и статистики."""
import time

from bot import stats_report
from tests.test_e2e import ADMIN_ID, feed, make_callback, make_message, stack  # noqa: F401

THEN = int(time.time()) - 10 * 86400


async def steps(deps, count=3):
    return [await deps.funnel.add_step(3600 * (i + 1), text=f"Пост {i + 1}", backfill=False) for i in range(count)]


async def person(deps, db, uid, *, material=True, sent=0):
    """Человек, получивший урок и `sent` постов прогрева подряд."""
    await deps.users.upsert(uid)
    if not material:
        return
    await db.execute("UPDATE users SET material_sent_at = ? WHERE tg_id = ?", (THEN, uid))
    await deps.funnel.enqueue(uid, now=THEN)
    for index in range(sent):
        head = await deps.funnel.head_row(uid)
        await deps.funnel.mark_sent(head["id"], now=THEN + 3600 * (index + 1))


async def ids(deps, segment, value=None):
    return sorted(await deps.users.segment_ids(segment, value))


async def test_person_without_material_is_neither_running_nor_done(stack, db):
    dp, bot, session, deps = stack
    await steps(deps)
    await person(deps, db, 1, material=False)
    assert await ids(deps, "funnel_done") == [] and await ids(deps, "funnel_running") == []
    assert await ids(deps, "no_material") == [1]


async def test_running_until_the_last_post_is_sent_then_done(stack, db):
    dp, bot, session, deps = stack
    await steps(deps, 3)
    await person(deps, db, 1, sent=0)
    await person(deps, db, 2, sent=2)
    await person(deps, db, 3, sent=3)
    assert await ids(deps, "funnel_running") == [1, 2]
    assert await ids(deps, "funnel_done") == [3]


async def test_skipped_and_failed_steps_do_not_hold_the_funnel(stack, db):
    """Кому остальные шаги пропущены (не подписан и т.п.) или они сорвались, ждать больше нечего."""
    dp, bot, session, deps = stack
    await steps(deps, 2)
    await person(deps, db, 1, sent=1)
    head = await deps.funnel.head_row(1)
    await deps.funnel.finish(head["id"], "skipped", "не подписан")
    await person(deps, db, 2, sent=1)
    head = await deps.funnel.head_row(2)
    await deps.funnel.finish(head["id"], "failed", "ошибка")
    assert await ids(deps, "funnel_done") == [1, 2]


async def test_disabled_pending_step_does_not_hold_the_funnel(stack, db):
    dp, bot, session, deps = stack
    first, second = await steps(deps, 2)
    await person(deps, db, 1, sent=1)
    await db.execute("UPDATE funnel_steps SET enabled = 0 WHERE id = ?", (second,))
    assert await ids(deps, "funnel_done") == [1]


async def test_funnel_without_steps_means_done_right_after_the_lesson(stack, db):
    dp, bot, session, deps = stack
    await person(deps, db, 1)
    assert await ids(deps, "funnel_done") == [1]


async def test_blocked_people_and_admins_are_in_no_funnel_segment(stack, db):
    dp, bot, session, deps = stack
    await steps(deps, 1)
    await person(deps, db, 1, sent=1)
    await person(deps, db, 2, sent=1)
    await deps.users.mark_blocked(2)
    await person(deps, db, ADMIN_ID, sent=1)
    assert await ids(deps, "funnel_done") == [1]
    stages = await deps.users.funnel_stages()
    assert stages["done"] == 1 and stages["running"] == 0 and stages["no_material"] == 0


async def test_new_step_added_to_the_funnel_brings_finished_people_back_into_progress(stack, db):
    """Они ещё получат новый пост — значит, они снова «в процессе», а не «закончили»."""
    dp, bot, session, deps = stack
    await steps(deps, 2)
    await person(deps, db, 1, sent=2)
    assert await ids(deps, "funnel_done") == [1]
    await deps.funnel.add_step(7200, text="Новый пост")   # backfill по умолчанию
    assert await ids(deps, "funnel_done") == [] and await ids(deps, "funnel_running") == [1]


async def test_stage_is_the_post_a_person_is_waiting_for(stack, db):
    dp, bot, session, deps = stack
    first, second, third = await steps(deps, 3)
    await person(deps, db, 1, sent=0)
    await person(deps, db, 2, sent=1)
    await person(deps, db, 3, sent=1)
    await person(deps, db, 4, sent=3)          # закончил — ни на каком посте не ждёт
    await person(deps, db, 5, material=False)
    assert await ids(deps, "step", str(first)) == [1]
    assert await ids(deps, "step", str(second)) == [2, 3]
    assert await ids(deps, "step", str(third)) == []
    assert await ids(deps, "step", "не число") == []

    stages = await deps.users.funnel_stages()
    assert (stages["no_material"], stages["running"], stages["done"]) == (1, 3, 1)
    assert [(s["number"], s["count"]) for s in stages["stages"]] == [(1, 1), (2, 2), (3, 0)]


async def test_categories_add_up_to_all_active_people(stack, db):
    dp, bot, session, deps = stack
    await steps(deps, 2)
    await person(deps, db, 1, material=False)
    await person(deps, db, 2, sent=1)
    await person(deps, db, 3, sent=2)
    stages = await deps.users.funnel_stages()
    assert stages["no_material"] + stages["running"] + stages["done"] == await deps.users.segment_count("all")


async def test_stats_show_the_categories_and_per_post_waiting(stack, db):
    dp, bot, session, deps = stack
    await steps(deps, 3)
    await person(deps, db, 1, material=False)
    await person(deps, db, 2, sent=1)
    await person(deps, db, 3, sent=1)
    await person(deps, db, 4, sent=3)
    text = await stats_report.build_report(deps)
    assert "Этапы воронки" in text
    assert "Ещё не получили материал — 1 (25%)" in text
    assert "В процессе прогрева — 2 (50%)" in text
    assert "ждут пост 2 (через 2 ч) — 2" in text
    assert "Закончили воронку — 1 (25%)" in text


async def test_stats_section_sits_right_after_the_funnel_block(stack, db):
    """Секции общие для Telegram-отчёта и листа «Сводка» в Excel (stats_export берёт их из sections())."""
    dp, bot, session, deps = stack
    await steps(deps, 1)
    await person(deps, db, 1, sent=1)
    data = await stats_report.collect(deps)
    titles = [title for title, _ in stats_report.sections(data)]
    assert "🧭 Этапы воронки" in titles
    assert titles.index("🧭 Этапы воронки") == titles.index("🔻 Воронка") + 1


# ---------------------------------------------------------------- рассылка


async def _start_draft(dp, bot):
    await feed(dp, bot, message=make_message("/admin", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:bc:new", user_id=ADMIN_ID))
    await feed(dp, bot, message=make_message("Для тех, кто прошёл воронку", user_id=ADMIN_ID, message_id=20))
    await feed(dp, bot, callback=make_callback("a:bc:done", user_id=ADMIN_ID))


def _buttons(session):
    """Подписи кнопок последнего показанного экрана."""
    for request in reversed(session.requests):
        markup = getattr(request, "reply_markup", None)
        if markup is not None and getattr(markup, "inline_keyboard", None):
            return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]
    return []


async def test_segment_picker_offers_finished_and_running_with_counts(stack, db):
    dp, bot, session, deps = stack
    await steps(deps, 2)
    await person(deps, db, 1, sent=2)
    await person(deps, db, 2, sent=2)
    await person(deps, db, 3, sent=1)
    await _start_draft(dp, bot)
    await feed(dp, bot, callback=make_callback("a:bc:tosend", user_id=ADMIN_ID))
    buttons = dict(_buttons(session))
    assert buttons["✅ Закончили воронку — 2"] == "a:bc:seg:funnel_done"
    assert buttons["🔄 В процессе прогрева — 1"] == "a:bc:seg:funnel_running"
    assert "a:bc:stages" in buttons.values()


async def test_broadcast_to_finished_goes_only_to_finished(stack, db):
    dp, bot, session, deps = stack
    await steps(deps, 2)
    await person(deps, db, 1, sent=2)
    await person(deps, db, 2, sent=1)
    await person(deps, db, 3, material=False)
    await _start_draft(dp, bot)
    await feed(dp, bot, callback=make_callback("a:bc:tosend", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:bc:seg:funnel_done", user_id=ADMIN_ID))
    broadcast = (await deps.broadcasts.recent(1))[0]
    assert broadcast["segment"] == "funnel_done"
    session.requests.clear()

    stats = await deps.engine.run(broadcast["id"])

    assert stats["sent"] == 1
    assert [m.chat_id for m in session.calls("SendMessage")] == [1]


async def test_stage_screen_lists_posts_with_people_and_sends_to_the_chosen_one(stack, db):
    dp, bot, session, deps = stack
    first, second = await steps(deps, 2)
    await person(deps, db, 1, sent=0)
    await person(deps, db, 2, sent=1)
    await person(deps, db, 3, sent=1)
    await person(deps, db, 4, sent=2)
    await _start_draft(dp, bot)
    await feed(dp, bot, callback=make_callback("a:bc:tosend", user_id=ADMIN_ID))
    await feed(dp, bot, callback=make_callback("a:bc:stages", user_id=ADMIN_ID))
    buttons = dict(_buttons(session))
    assert buttons["Ждут пост 1 (через 1 ч) — 1"] == f"a:bc:stp:{first}"
    assert buttons["Ждут пост 2 (через 2 ч) — 2"] == f"a:bc:stp:{second}"

    await feed(dp, bot, callback=make_callback(f"a:bc:stp:{second}", user_id=ADMIN_ID))
    broadcast = (await deps.broadcasts.recent(1))[0]
    assert (broadcast["segment"], broadcast["segment_value"]) == ("step", str(second))
    targets = await deps.broadcasts.pending_targets(broadcast["id"], 100)
    assert sorted(targets) == [2, 3]
    card = [r.text for r in session.calls("SendMessage") + session.calls("EditMessageText") if "Рассылка #" in (r.text or "")]
    assert card and "Ждут пост 2 (через 2 ч) — 2" in card[-1]


async def test_stage_screen_without_waiting_people_says_so(stack, db):
    dp, bot, session, deps = stack
    await steps(deps, 1)
    await person(deps, db, 1, sent=1)
    await _start_draft(dp, bot)
    await feed(dp, bot, callback=make_callback("a:bc:stages", user_id=ADMIN_ID))
    texts = [r.text for r in session.calls("SendMessage") + session.calls("EditMessageText") if r.text]
    assert "никто не ждёт" in texts[-1]
    assert [data for _, data in _buttons(session)] == ["a:bc:tosend"]
