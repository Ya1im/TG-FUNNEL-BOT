"""Предпрод: пометки «когда пришло бы человеку», перестройка очереди при запуске и выпуске."""
from bot.preprod import human_span, init_chain, timing_note
from bot.repo.funnel import NOT_SCHEDULED, FunnelRepo
from bot.repo.sentlog import SentLogRepo
from bot.repo.settings import SettingsRepo
from bot.repo.users import UsersRepo
from bot.scheduler import Scheduler

H = 3600


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, disable_notification=None):
        self.sent.append((chat_id, text, disable_notification))
        return type("M", (), {"message_id": 100 + len(self.sent)})()


class Gate:
    async def status(self, *a, **k):
        return "yes"


async def setup(db, users=(1,), delays=(1800, 12 * H, 12 * H)):
    urepo, funnel = UsersRepo(db), FunnelRepo(db)
    for uid in users:
        await urepo.upsert(uid, "u", "В")
        await db.execute("UPDATE users SET material_sent_at = 0 WHERE tg_id = ?", (uid,))
    ids = [await funnel.add_step(d, text=f"Пост {i}", backfill=False) for i, d in enumerate(delays, 1)]
    for uid in users:
        await funnel.enqueue(uid, now=0)
    return urepo, funnel, ids


def test_human_span():
    assert human_span(1800) == "30 мин"
    assert human_span(12 * H) == "12 ч"
    assert human_span(86400 + 20 * H + 1800) == "1 д 20 ч 30 мин"


async def test_timing_note_texts(db):
    _, funnel, ids = await setup(db)
    first = await timing_note(funnel, ids[0])
    third = await timing_note(funnel, ids[2])
    assert "пост 1 из 3" in first and "через 30 мин после получения урока" in first
    assert "пост 3 из 3" in third
    assert "через 12 ч после предыдущего поста" in third
    assert "≈ через 1 д 30 мин после урока" in third                # 30 мин + 12 ч + 12 ч
    assert await timing_note(funnel, 9999) is None


async def test_timing_note_skips_disabled_steps(db):
    _, funnel, ids = await setup(db)
    await funnel.update_step(ids[1], enabled=0)
    note = await timing_note(funnel, ids[2])
    assert "пост 2 из 2" in note


async def test_preprod_sends_note_before_post_and_journals_it(db):
    users, funnel, ids = await setup(db)
    await SettingsRepo(db).set("preprod_mode", "1")
    await SettingsRepo(db).set("preprod_user_ids", "1")
    bot, log = Bot(), SentLogRepo(db)
    sched = Scheduler(bot=bot, users=users, funnel=funnel, settings=SettingsRepo(db), gate=Gate(),
                      sent_log=log, now=lambda: 5000)
    assert (await sched.tick())["sent"] == 1
    assert [t for _, t, _ in bot.sent][1] == "Пост 1"
    note = bot.sent[0]
    assert "Предпрод" in note[1] and note[2] is True             # беззвучно
    assert len(await log.since(0)) == 2                          # и пометка, и пост можно отозвать


async def test_no_note_when_preprod_is_off(db):
    users, funnel, ids = await setup(db)
    bot = Bot()
    sched = Scheduler(bot=bot, users=users, funnel=funnel, settings=SettingsRepo(db), gate=Gate(),
                      now=lambda: 5000)
    await sched.tick()
    assert [t for _, t, _ in bot.sent] == ["Пост 1"]


async def test_no_note_for_retry_attempt(db):
    users, funnel, ids = await setup(db)
    await db.execute("UPDATE user_steps SET attempts = 1")
    bot = Bot()
    sched = Scheduler(bot=bot, users=users, funnel=funnel, settings=SettingsRepo(db), gate=Gate(),
                      now=lambda: 5000)
    row = (await funnel.due_steps(5000))[0]
    await sched._process(row, {"sent": 0, "held": 0, "skipped": 0, "failed": 0, "blocked": 0}, set(), preprod=True)
    assert [t for _, t, _ in bot.sent] == ["Пост 1"]


async def test_init_chain_rebases_once_and_never_sends_burst(db):
    """Старая очередь: у всех просрочено. После перестройки — отсчёт от запуска, залпа нет."""
    users, funnel, ids = await setup(db, users=(1, 2, 3))
    settings = SettingsRepo(db)
    # имитация старой схемы: у всех строк сроки в прошлом
    await db.execute("UPDATE user_steps SET due_at = 100")
    assert await init_chain(funnel, settings, now=1_000_000) == 3
    assert await init_chain(funnel, settings, now=2_000_000) == 0           # повторно — ничего
    rows = await db.fetchall("SELECT DISTINCT due_at FROM user_steps WHERE due_at < ?", (NOT_SCHEDULED,))
    assert [r["due_at"] for r in rows] == [1_000_000 + 1800]
    bot = Bot()
    sched = Scheduler(bot=bot, users=users, funnel=funnel, settings=settings, gate=Gate(), now=lambda: 1_000_001)
    assert (await sched.tick())["sent"] == 0


async def test_note_sent_once_even_after_postponed_attempt(db):
    """Шаг с подпиской: до отправки поста были попытки (attempts>0), пометка всё равно должна прийти — один раз."""
    users, funnel, ids = await setup(db)
    await db.execute("UPDATE user_steps SET attempts = 2")
    await db.execute("UPDATE users SET started_at = 0")
    bot, log = Bot(), SentLogRepo(db)
    sched = Scheduler(bot=bot, users=users, funnel=funnel, settings=SettingsRepo(db), gate=Gate(),
                      sent_log=log, now=lambda: 5000)
    row = (await funnel.due_steps(5000))[0]
    stats = {"sent": 0, "held": 0, "skipped": 0, "failed": 0, "blocked": 0}
    await sched._process(row, stats, set(), preprod=True)
    assert [t for _, t, _ in bot.sent][0].startswith("🧪")
    assert await sched._note_already_sent(row) is True


async def test_tester_ids_include_owners_role_admins_and_listed(db):
    from bot.preprod import allowed_user_ids, tester_ids
    await db.execute("INSERT INTO viewers(tg_id, name, added_at, role) VALUES(50, 'a', 0, 'admin')")
    await db.execute("INSERT INTO viewers(tg_id, name, added_at, role) VALUES(51, 's', 0, 'stats')")
    settings = SettingsRepo(db)
    await settings.set("preprod_user_ids", "7, 8")
    assert await tester_ids(settings, (99,), db) == {7, 8, 99, 50}
    assert await allowed_user_ids(settings, (99,), db) is None                 # предпрод выключен
    await settings.set("preprod_mode", "1")
    assert await allowed_user_ids(settings, (99,), db) == {7, 8, 99, 50}


async def test_note_comes_again_after_restart_of_the_run(db):
    """«Начать заново»: журнал прошлого прохождения не должен глушить пометки в новом."""
    users, funnel, ids = await setup(db)
    await db.execute("UPDATE users SET started_at = 0")
    log = SentLogRepo(db)
    await log.add(1, [1, 2], "step", ids[0], now=100)
    sched = Scheduler(bot=Bot(), users=users, funnel=funnel, settings=SettingsRepo(db), gate=Gate(),
                      sent_log=log, now=lambda: 5000)
    row = (await funnel.due_steps(5000))[0]
    assert await sched._note_already_sent(row) is True
    await db.execute("UPDATE users SET started_at = 1000")            # сброс прохождения
    assert await sched._note_already_sent(row) is False
