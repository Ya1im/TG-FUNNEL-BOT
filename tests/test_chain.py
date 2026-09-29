"""Цепочка «через сколько после предыдущего поста»: срок следующего шага считается от фактической отправки."""
from bot.repo.funnel import FAST_STEP_SECONDS, NOT_SCHEDULED, FunnelRepo
from bot.repo.settings import SettingsRepo
from bot.repo.users import UsersRepo
from bot.scheduler import Scheduler

H = 3600


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, disable_notification=None):
        self.sent.append((chat_id, text))
        return type("M", (), {"message_id": len(self.sent)})()


class Gate:
    async def status(self, *a, **k):
        return "yes"


async def user(db, tg_id=1, material_sent_at=0):
    await UsersRepo(db).upsert(tg_id, "u", "В")
    await db.execute("UPDATE users SET material_sent_at = ? WHERE tg_id = ?", (material_sent_at, tg_id))


async def dues(db, user_id=1):
    rows = await db.fetchall(
        "SELECT fs.position, us.due_at, us.status FROM user_steps us JOIN funnel_steps fs ON fs.id = us.step_id "
        "WHERE us.user_id = ? ORDER BY fs.position", (user_id,))
    return [(r["due_at"], r["status"]) for r in rows]


async def steps(funnel, *delays, **kw):
    return [await funnel.add_step(d, text=f"Пост {i}", backfill=False, **kw) for i, d in enumerate(delays, 1)]


async def test_next_step_counts_from_actual_send_time(db):
    funnel = FunnelRepo(db)
    await user(db)
    await steps(funnel, 30 * 60, 2 * H, 6 * H)
    await funnel.enqueue(1, now=0)
    assert await dues(db) == [(1800, "pending"), (NOT_SCHEDULED, "pending"), (NOT_SCHEDULED, "pending")]
    # бот был выключен и отправил пост с опозданием — следующий считается от факта, а не от плана
    queue_id = (await funnel.due_steps(now=5000))[0]["queue_id"]
    await funnel.mark_sent(queue_id, now=5000)
    assert await dues(db) == [(1800, "sent"), (5000 + 2 * H, "pending"), (NOT_SCHEDULED, "pending")]


async def test_old_user_gets_only_one_step_not_five(db):
    """Получил урок неделю назад: все пять пушей «просрочены», но уйдёт один, остальные — по цепочке."""
    funnel = FunnelRepo(db)
    await user(db, material_sent_at=0)
    await steps(funnel, 1800, 7200, 21600, 43200, 43200)
    await funnel.enqueue(1, now=0)
    week = 7 * 86400
    assert len(await funnel.due_steps(now=week)) == 1
    queue_id = (await funnel.due_steps(now=week))[0]["queue_id"]
    await funnel.mark_sent(queue_id, now=week)
    assert await funnel.due_steps(now=week) == []
    assert len(await funnel.due_steps(now=week + 7200)) == 1


async def test_scheduler_sends_one_message_per_user_per_burst(db):
    funnel = FunnelRepo(db)
    for uid in range(1, 11):
        await user(db, uid)
    await steps(funnel, 1800, 7200, 21600, 43200, 43200)
    for uid in range(1, 11):
        await funnel.enqueue(uid, now=0)
    bot = Bot()
    now = {"t": 7 * 86400}
    sched = Scheduler(bot=bot, users=UsersRepo(db), funnel=funnel, settings=SettingsRepo(db), gate=Gate(),
                      now=lambda: now["t"])
    assert (await sched.tick())["sent"] == 10
    assert (await sched.tick())["sent"] == 0           # второй пост никому не уходит сразу
    now["t"] += 7200
    assert (await sched.tick())["sent"] == 10          # через свою задержку — второй


async def test_skipped_step_starts_next_from_skip_moment(db):
    funnel = FunnelRepo(db)
    await user(db)
    await steps(funnel, 100, 500)
    await funnel.enqueue(1, now=0)
    queue_id = (await funnel.due_steps(now=100))[0]["queue_id"]
    await funnel.finish(queue_id, "skipped", "нет подписки", now=2000)
    assert await dues(db) == [(100, "skipped"), (2500, "pending")]


async def test_postponed_head_holds_the_chain(db):
    funnel = FunnelRepo(db)
    await user(db)
    await steps(funnel, 100, 500)
    await funnel.enqueue(1, now=0)
    queue_id = (await funnel.due_steps(now=100))[0]["queue_id"]
    await funnel.postpone(queue_id, 6 * H)
    rows = await dues(db)
    assert rows[0][1] == "pending" and rows[0][0] > 100      # головной шаг ждёт свой новый срок
    assert rows[1] == (NOT_SCHEDULED, "pending")             # а цепочка не идёт дальше него
    assert await funnel.due_steps(now=100_000) == []


async def test_appended_step_for_finished_user_counts_from_adding(db):
    funnel = FunnelRepo(db)
    await user(db)
    [first] = await steps(funnel, 100)
    await funnel.enqueue(1, now=0)
    await funnel.mark_sent((await funnel.due_steps(now=100))[0]["queue_id"], now=100)
    new_id = await funnel.add_step(7200, text="Новый", backfill=False)
    await funnel.backfill_step(new_id, now=10_000)
    assert (await dues(db))[1] == (17_200, "pending")


async def test_appended_step_for_mid_chain_user_waits_its_turn(db):
    funnel = FunnelRepo(db)
    await user(db)
    await steps(funnel, 100, 200)
    await funnel.enqueue(1, now=0)
    new_id = await funnel.add_step(7200, text="Новый", backfill=False)
    await funnel.backfill_step(new_id, now=50)
    assert await dues(db) == [(100, "pending"), (NOT_SCHEDULED, "pending"), (NOT_SCHEDULED, "pending")]


async def test_disabled_head_does_not_block_chain(db):
    funnel = FunnelRepo(db)
    await user(db)
    a, b = await steps(funnel, 100, 500)
    await funnel.enqueue(1, now=0)
    await funnel.update_step(a, enabled=0)
    assert (await dues(db))[1][0] != NOT_SCHEDULED     # второй шаг стал головным
    await funnel.update_step(a, enabled=1)             # включили обратно — снова первый по порядку
    assert (await dues(db))[0][0] != NOT_SCHEDULED and (await dues(db))[1][0] == NOT_SCHEDULED


async def test_reenabled_step_is_not_revived_for_users_who_moved_on(db):
    funnel = FunnelRepo(db)
    await user(db)
    a, b = await steps(funnel, 100, 500)
    await funnel.enqueue(1, now=0)
    await funnel.update_step(a, enabled=0)
    await funnel.mark_sent((await funnel.due_steps(now=2_000_000_000))[0]["queue_id"], now=1000)   # получил б
    await funnel.update_step(a, enabled=1)
    assert (await dues(db))[0][1] == "skipped"


async def test_delete_head_step_promotes_next(db):
    funnel = FunnelRepo(db)
    await user(db)
    a, b = await steps(funnel, 100, 500)
    await funnel.enqueue(1, now=0)
    await funnel.delete_step(a)
    assert (await dues(db))[0][0] != NOT_SCHEDULED


async def test_move_step_changes_head(db):
    funnel = FunnelRepo(db)
    await user(db)
    a, b = await steps(funnel, 100, 500)
    await funnel.enqueue(1, now=0)
    await funnel.move_step(b, -1)
    rows = await db.fetchall("SELECT step_id, due_at FROM user_steps ORDER BY due_at")
    assert rows[0]["step_id"] == b and rows[1]["due_at"] == NOT_SCHEDULED


async def test_rebase_overdue_heads_counts_from_launch(db):
    funnel = FunnelRepo(db)
    for uid in (1, 2):
        await user(db, uid)
    await steps(funnel, 1800, 7200)
    await funnel.enqueue(1, now=0)
    await funnel.enqueue(2, now=10**6)                        # у второго срок в будущем
    rebased = await funnel.rebase_overdue_heads(now=500_000)
    assert rebased == 1
    assert (await dues(db, 1))[0][0] == 500_000 + 1800
    assert (await dues(db, 2))[0][0] == 10**6 + 1800          # будущий срок не трогаем


async def test_fast_mode_chain_is_ten_seconds_per_step(db):
    funnel = FunnelRepo(db)
    await user(db)
    await steps(funnel, 86400, 86400)
    await funnel.enqueue(1, fast=True, now=0)
    assert (await dues(db))[0][0] == FAST_STEP_SECONDS
    await funnel.mark_sent((await funnel.due_steps(now=100))[0]["queue_id"], now=100)
    assert (await dues(db))[1][0] == 100 + FAST_STEP_SECONDS


async def test_heal_stranded_restores_chain_after_crash(db):
    """Сбой между «шаг отправлен» и «назначен срок следующему»: у человека ни одного срока."""
    funnel = FunnelRepo(db)
    await user(db)
    await steps(funnel, 100, 500)
    await funnel.enqueue(1, now=0)
    await db.execute("UPDATE user_steps SET status = 'sent', sent_at = 100 WHERE id = (SELECT MIN(id) FROM user_steps)")
    assert await funnel.due_steps(now=8_000_000_000) == []      # застряли
    assert await funnel.heal_stranded(now=1000) == 1
    assert (await dues(db))[1][0] == 1500
    assert await funnel.heal_stranded(now=1000) == 0             # здоровых не трогает


async def test_scheduler_heals_on_first_tick(db):
    funnel = FunnelRepo(db)
    await user(db)
    await steps(funnel, 100, 500)
    await funnel.enqueue(1, now=0)
    await db.execute("UPDATE user_steps SET status = 'sent', sent_at = 100 WHERE id = (SELECT MIN(id) FROM user_steps)")
    bot = Bot()
    sched = Scheduler(bot=bot, users=UsersRepo(db), funnel=funnel, settings=SettingsRepo(db), gate=Gate(),
                      now=lambda: 1000)
    await sched.tick()
    assert (await dues(db))[1][0] == 1500
