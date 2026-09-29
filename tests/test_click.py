"""Клик по кнопке урока: запись первого клика и ветвление воронки."""
import json
from types import SimpleNamespace

from bot.content import button_hash
from bot.repo.funnel import FAST_STEP_SECONDS, FunnelRepo, block_from_row
from bot.repo.material import MaterialRepo
from bot.repo.users import UsersRepo
from bot.services import find_tracked_url, record_click

T0 = 1_000_000
H = 3600
URL = "https://tkpdt.ru/lendingi150"

PUSHES = [30 * 60, 2 * H + 30 * 60, 8 * H + 30 * 60, 20 * H + 30 * 60, 32 * H + 30 * 60]
ANCHOR = 44 * H + 30 * 60
TAIL = [56 * H + 30 * 60, 68 * H + 30 * 60]


async def build(db, fast=False):
    users, funnel = UsersRepo(db), FunnelRepo(db)
    await users.upsert(1, "u", "Вася")
    push_ids = [await funnel.add_step(d, text=f"П{i}", stop_on_click=True) for i, d in enumerate(PUSHES, 1)]
    anchor_id = await funnel.add_step(ANCHOR, text="Призыв", after_click_seconds=H)
    tail_ids = [await funnel.add_step(d, text=f"С{i}") for i, d in enumerate(TAIL, 1)]
    # материал «выдан» после создания шагов — очередь строит именно enqueue, а не backfill
    await db.execute("UPDATE users SET material_sent_at = ? WHERE tg_id = 1", (T0,))
    await funnel.enqueue(1, fast=fast, now=T0)
    deps = SimpleNamespace(users=users, funnel=funnel)
    return deps, push_ids, anchor_id, tail_ids


async def queue(db):
    rows = await db.fetchall(
        "SELECT us.step_id, us.due_at, us.status, us.last_error FROM user_steps us "
        "JOIN funnel_steps fs ON fs.id = us.step_id WHERE us.user_id = 1 ORDER BY fs.position"
    )
    return {r["step_id"]: r for r in rows}


async def test_click_skips_stop_on_click_steps(db):
    deps, pushes, anchor, tail = await build(db)
    assert await record_click(deps, 1, now=T0 + 600) is True
    q = await queue(db)
    for sid in pushes:
        assert q[sid]["status"] == "skipped"
        assert q[sid]["last_error"] == "клик по уроку"
    assert q[anchor]["status"] == "pending"


async def test_click_only_skips_pending_pushes(db):
    deps, pushes, anchor, tail = await build(db)
    await db.execute("UPDATE user_steps SET status = 'sent' WHERE step_id IN (?, ?)", (pushes[0], pushes[1]))
    await record_click(deps, 1, now=T0 + 3 * H)
    q = await queue(db)
    assert q[pushes[0]]["status"] == "sent" and q[pushes[1]]["status"] == "sent"
    assert all(q[s]["status"] == "skipped" for s in pushes[2:])


async def test_click_reanchors_chain_and_preserves_spacing(db):
    deps, pushes, anchor, tail = await build(db)
    click = T0 + 600
    await record_click(deps, 1, now=click)
    q = await queue(db)
    assert q[anchor]["due_at"] == click + H
    assert q[tail[0]]["due_at"] - q[anchor]["due_at"] == TAIL[0] - ANCHOR
    assert q[tail[1]]["due_at"] - q[anchor]["due_at"] == TAIL[1] - ANCHOR


async def test_click_after_last_push_before_anchor_moves_chain(db):
    deps, pushes, anchor, tail = await build(db)
    await db.execute("UPDATE user_steps SET status = 'sent' WHERE step_id IN (%s)" % ",".join(map(str, pushes)))
    click = T0 + 44 * H
    await record_click(deps, 1, now=click)
    q = await queue(db)
    assert q[anchor]["due_at"] == click + H          # сдвиг вперёд на 30 минут
    assert q[tail[0]]["due_at"] == T0 + TAIL[0] + (click + H - (T0 + ANCHOR))
    assert all(q[s]["status"] == "sent" for s in pushes)


async def test_click_after_anchor_sent_does_not_shift(db):
    deps, pushes, anchor, tail = await build(db)
    await db.execute("UPDATE user_steps SET status = 'sent' WHERE step_id = ?", (anchor,))
    before = await queue(db)
    await record_click(deps, 1, now=T0 + 50 * H)
    after = await queue(db)
    assert after[tail[0]]["due_at"] == before[tail[0]]["due_at"]
    assert after[tail[1]]["due_at"] == before[tail[1]]["due_at"]
    assert all(after[s]["status"] == "skipped" for s in pushes)  # отправленный призыв, но пуши уже не нужны


async def test_click_after_anchor_skipped_does_not_shift(db):
    deps, pushes, anchor, tail = await build(db)
    await db.execute("UPDATE user_steps SET status = 'skipped' WHERE step_id = ?", (anchor,))
    before = await queue(db)
    await record_click(deps, 1, now=T0 + 600)
    after = await queue(db)
    assert after[tail[0]]["due_at"] == before[tail[0]]["due_at"]


async def test_double_click_is_idempotent(db):
    deps, pushes, anchor, tail = await build(db)
    assert await record_click(deps, 1, now=T0 + 600) is True
    first = await queue(db)
    assert await record_click(deps, 1, now=T0 + 5 * H) is False
    second = await queue(db)
    assert {k: v["due_at"] for k, v in first.items()} == {k: v["due_at"] for k, v in second.items()}
    assert (await deps.users.get(1))["lesson_clicked_at"] == T0 + 600


async def test_click_without_queue_only_records_time(db):
    users, funnel = UsersRepo(db), FunnelRepo(db)
    await users.upsert(1, "u", "В")
    deps = SimpleNamespace(users=users, funnel=funnel)
    assert await record_click(deps, 1, now=500) is True
    assert (await users.get(1))["lesson_clicked_at"] == 500


async def test_click_from_unknown_user_is_noop(db):
    deps = SimpleNamespace(users=UsersRepo(db), funnel=FunnelRepo(db))
    assert await record_click(deps, 42, now=500) is False


async def test_click_in_fast_mode(db):
    deps, pushes, anchor, tail = await build(db, fast=True)
    assert (await deps.users.get(1))["funnel_fast"] == 1
    click = T0 + 5
    await record_click(deps, 1, now=click)
    q = await queue(db)
    assert q[anchor]["due_at"] == click + FAST_STEP_SECONDS
    assert q[tail[0]]["due_at"] - q[anchor]["due_at"] == FAST_STEP_SECONDS  # быстрый интервал сохранён


async def test_enqueue_normal_clears_fast_flag(db):
    deps, *_ = await build(db, fast=True)
    await deps.funnel.clear_user(1)
    await deps.funnel.enqueue(1, now=T0)
    assert (await deps.users.get(1))["funnel_fast"] == 0


async def test_export_import_roundtrip_new_fields(db):
    from bot.repo.media import MediaRepo

    funnel = FunnelRepo(db)
    await funnel.add_step(
        60, text="п", stop_on_click=True, buttons=[{"text": "Урок", "url": URL, "track": True}]
    )
    await funnel.add_step(120, text="призыв", after_click_seconds=3600)
    raw = await funnel.export_json()
    await funnel.import_json(raw, MediaRepo(db))
    steps = await funnel.list_steps()
    assert [s["stop_on_click"] for s in steps] == [1, 0]
    assert [s["after_click_seconds"] for s in steps] == [None, 3600]
    assert json.loads(steps[0]["buttons_json"])[0]["track"] is True


async def test_old_export_without_new_fields_imports(db):
    from bot.repo.media import MediaRepo

    funnel = FunnelRepo(db)
    await funnel.import_json(json.dumps([{"delay_seconds": 60, "text": "x"}]), MediaRepo(db))
    step = (await funnel.list_steps())[0]
    assert step["stop_on_click"] == 0 and step["after_click_seconds"] is None


async def test_find_tracked_url(db):
    funnel, material = FunnelRepo(db), MaterialRepo(db)
    await funnel.add_step(60, text="п", buttons=[{"text": "Урок", "url": URL, "track": True}])
    await material.add_block(text="м", buttons=[{"text": "Разбор", "url": "https://t.me/m/x", "track": True}])
    deps = SimpleNamespace(funnel=funnel, material=material)
    assert await find_tracked_url(deps, button_hash(URL)) == URL
    assert await find_tracked_url(deps, button_hash("https://t.me/m/x")) == "https://t.me/m/x"
    assert await find_tracked_url(deps, "deadbeefdeadbeef") is None


async def test_find_tracked_url_survives_removed_track_flag(db):
    """Админ убрал «| клик», но старые сообщения с callback-кнопкой ещё живы — ссылка не должна умереть."""
    funnel, material = FunnelRepo(db), MaterialRepo(db)
    await funnel.add_step(60, text="п", buttons=[{"text": "Урок", "url": URL}])
    deps = SimpleNamespace(funnel=funnel, material=material)
    assert await find_tracked_url(deps, button_hash(URL)) == URL


async def test_block_from_row_track_flag(db):
    funnel = FunnelRepo(db)
    await funnel.add_step(60, text="п", buttons=[{"text": "Урок", "url": URL, "track": True}])
    row = (await funnel.list_steps())[0]
    assert block_from_row(row).keyboard().inline_keyboard[0][0].url == URL
    assert block_from_row(row, track=True).keyboard().inline_keyboard[0][0].callback_data.startswith("lc:")


async def test_click_reapplied_when_it_happened_before_queue_was_built(db):
    """Кнопка уходит в материале раньше, чем строится очередь: клик в этом окне не должен потеряться."""
    from types import SimpleNamespace as NS

    from bot.services import deliver_material

    users, funnel = UsersRepo(db), FunnelRepo(db)
    await users.upsert(1, "u", "В")
    await funnel.add_step(1800, text="П", stop_on_click=True)
    await funnel.add_step(3600, text="Призыв", after_click_seconds=3600)
    await users.mark_lesson_clicked(1, 5000)   # клик успел раньше enqueue
    from bot.repo.settings import SettingsRepo
    from bot.sender import RateLimiter

    class Bot:
        async def send_message(self, *a, **k):
            return "m"

    deps = NS(users=users, funnel=funnel, material=MaterialRepo(db), settings=SettingsRepo(db), limiter=RateLimiter(0))
    assert await deliver_material(Bot(), deps, 1) is True
    q = await queue(db)
    statuses = sorted(r["status"] for r in q.values())
    assert statuses == ["pending", "skipped"]


async def test_backfill_skips_stop_on_click_step_for_clicked_users(db):
    users, funnel = UsersRepo(db), FunnelRepo(db)
    await users.upsert(1, "u", "В")
    await users.upsert(2, "u", "Г")
    for uid in (1, 2):
        await db.execute("UPDATE users SET material_sent_at = ? WHERE tg_id = ?", (T0, uid))
    await users.mark_lesson_clicked(1, T0 + 5)
    push = await funnel.add_step(60, text="пуш", stop_on_click=True)   # backfill внутри add_step
    plain = await funnel.add_step(60, text="обычный")
    rows = await db.fetchall("SELECT user_id, step_id FROM user_steps ORDER BY user_id, step_id")
    assert {(r["user_id"], r["step_id"]) for r in rows} == {(1, plain), (2, push), (2, plain)}


async def test_apply_click_never_moves_later_steps_before_anchor(db):
    users, funnel = UsersRepo(db), FunnelRepo(db)
    await users.upsert(1, "u", "В")
    anchor = await funnel.add_step(5000, text="призыв", after_click_seconds=3600)
    early_tail = await funnel.add_step(1000, text="хвост с малой задержкой")  # порядок расходится с задержками
    await db.execute("UPDATE users SET material_sent_at = ? WHERE tg_id = 1", (T0,))
    await funnel.enqueue(1, now=T0)
    await record_click(SimpleNamespace(users=users, funnel=funnel), 1, now=T0 + 10)
    q = await queue(db)
    assert q[early_tail]["due_at"] >= q[anchor]["due_at"]


async def test_failed_apply_click_rolls_back_click_mark(db, monkeypatch):
    import pytest as _pytest

    deps, *_ = await build(db)

    async def boom(*a, **k):
        raise RuntimeError("db")

    monkeypatch.setattr(deps.funnel, "apply_click", boom)
    with _pytest.raises(RuntimeError):
        await record_click(deps, 1, now=T0 + 5)
    assert (await deps.users.get(1))["lesson_clicked_at"] is None   # следующий клик попробует снова


async def test_enqueue_repeat_does_not_reset_fast_flag(db):
    deps, *_ = await build(db, fast=True)
    assert await deps.funnel.enqueue(1, now=T0) == 0     # повтор ничего не создал
    assert (await deps.users.get(1))["funnel_fast"] == 1
