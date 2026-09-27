# Независимый бэкфилл новых шагов прогрева Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or implement natively task-by-task with test-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** новый (или снова включённый) шаг прогрева автоматически попадает в очередь всем, кто уже
получил материал, с `due_at`, посчитанным от личной даты каждого — без дублей и без рассылки не по
порядку тем, кто уже прошёл дальше.

**Architecture:** один новый метод `FunnelRepo.backfill_step`, чистый декларативный SQL (`INSERT OR
IGNORE ... SELECT ... WHERE NOT EXISTS ...`), вызывается из `add_step` и из `update_step` при переходе
`enabled` 0→1. Ни `bot/scheduler.py`, ни `due_steps()`, ни отправка не меняются.

**Tech Stack:** Python 3.11, aiosqlite, pytest (`./.venv-agent/bin/python -m pytest -q`).

**Spec:** `docs/superpowers/specs/2026-09-27-funnel-step-backfill-design.md`

## Global Constraints

- `due_at = users.material_sent_at (свой) + funnel_steps.delay_seconds (нового шага)` — не `now + delay`
  (спека §3).
- Пользователю не ставится новый шаг, если он уже получил (`user_steps.status = 'sent'`) шаг с более
  поздней позицией (`funnel_steps.position` строго больше позиции нового) — спека §3.
  `'skipped'` в счёт «уже дальше» не идёт.
- Только `users.material_sent_at IS NOT NULL` и `users.status = 'active'` — не активным (заблокировавшим
  бота) новую запись не заводим (естественное расширение спеки: `due_steps()` их и так не заберёт,
  но не плодим мёртвые строки).
- Бэкфилл срабатывает на создании шага и на переходе выключен→включён; на любых других правках шага
  (текст/медиа/кнопки/задержка/позиция/включён→выключен) — не срабатывает.
- Импорт цепочки (`import_json`) и обнуление статистики поведение не меняют (спека §5) — трогать не надо.

## Review Focus

- Пользователь с материалом, выданным несколько дней назад — новый шаг должен получить `due_at` в
  прошлом и быть отправлен на ближайшем тике планировщика, а не пропущен как «просроченный».
- Правка текста/медиа/кнопок/задержки/позиции уже существующего шага не должна вызывать бэкфилл —
  наивный хук «на любой `update_step`» тут ошибётся.
- Пользователь, чей более поздний шаг стоит в статусе `'skipped'` (не дошёл, например из-за подписки) —
  не считается «уже дальше», должен получить вставленный «в середину» новый шаг.
- Заблокировавший бота пользователь (`status='blocked'`) с уже выданным материалом — не должен получить
  новую запись в очереди при бэкфилле.
- Пользователь без `material_sent_at` (ещё не дошёл до материала) — не получает строку от бэкфилла;
  ему всё как обычно поставит текущий `enqueue()` в момент выдачи материала.

---

## Task 1: `backfill_step` + вызов из `add_step`

**Files:**
- Modify: `bot/repo/funnel.py:20-46` (`add_step`)
- Test: `tests/test_funnel.py`

**Interfaces:**
- Produces: `FunnelRepo.backfill_step(self, step_id: int, position: int, delay_seconds: int) -> int`
  (возвращает число добавленных строк `user_steps`; переиспользуется Task 2)
- Consumes: ничего нового — `self.db.execute/fetchval`, существующая схема `users`/`funnel_steps`/`user_steps`.

- [ ] **Step 1: Написать тесты** в `tests/test_funnel.py`:

```python
async def test_add_step_backfills_existing_users_from_their_own_material_time(db):
    users = UsersRepo(db)
    funnel = FunnelRepo(db)
    await users.upsert(1)
    await db.execute("UPDATE users SET material_sent_at = ? WHERE tg_id = 1", (1000 - 3 * 86400,))
    await users.upsert(2)
    await db.execute("UPDATE users SET material_sent_at = ? WHERE tg_id = 2", (1000 - 3600,))

    step_id = await funnel.add_step(delay_seconds=7200, text="Новый шаг")

    rows = {r["user_id"]: r["due_at"] for r in await db.fetchall(
        "SELECT user_id, due_at FROM user_steps WHERE step_id = ?", (step_id,)
    )}
    assert rows[1] == 1000 - 3 * 86400 + 7200   # у давнего due_at уже в прошлом
    assert rows[2] == 1000 - 3600 + 7200         # у недавнего — своя дата + задержка


async def test_add_step_does_not_backfill_user_without_material(db):
    users, funnel = UsersRepo(db), FunnelRepo(db)
    await users.upsert(1)  # material_sent_at ещё NULL
    step_id = await funnel.add_step(delay_seconds=3600, text="Шаг")
    assert await db.fetchall("SELECT 1 FROM user_steps WHERE step_id = ?", (step_id,)) == []


async def test_add_step_skips_blocked_users(db):
    users, funnel = UsersRepo(db), FunnelRepo(db)
    await users.upsert(1)
    await db.execute("UPDATE users SET material_sent_at = 1000 WHERE tg_id = 1")
    await users.mark_blocked(1)
    step_id = await funnel.add_step(delay_seconds=3600, text="Шаг")
    assert await db.fetchall("SELECT 1 FROM user_steps WHERE step_id = ?", (step_id,)) == []


async def test_add_step_mid_sequence_skips_users_already_past_it(db):
    users, funnel = UsersRepo(db), FunnelRepo(db)
    await users.upsert(1)
    await users.upsert(2)
    await db.execute("UPDATE users SET material_sent_at = 0 WHERE tg_id IN (1, 2)")
    later_id = await funnel.add_step(delay_seconds=100, text="Шаг №2 (уже есть)")
    await db.execute(
        "UPDATE user_steps SET status = 'sent' WHERE step_id = ? AND user_id = 1", (later_id,)
    )  # пользователь 1 уже получил более поздний шаг, пользователь 2 — ещё нет (pending)

    mid_id = await funnel.add_step(delay_seconds=50, text="Шаг №1.5 (вставили позже)")

    got_mid = {r["user_id"] for r in await db.fetchall(
        "SELECT user_id FROM user_steps WHERE step_id = ?", (mid_id,)
    )}
    assert got_mid == {2}  # пользователю 1 «более ранний» шаг задним числом не пришёл


async def test_add_step_treats_skipped_later_step_as_not_ahead(db):
    users, funnel = UsersRepo(db), FunnelRepo(db)
    await users.upsert(1)
    await db.execute("UPDATE users SET material_sent_at = 0 WHERE tg_id = 1")
    later_id = await funnel.add_step(delay_seconds=100, text="Более поздний")
    await db.execute(
        "UPDATE user_steps SET status = 'skipped' WHERE step_id = ? AND user_id = 1", (later_id,)
    )

    mid_id = await funnel.add_step(delay_seconds=50, text="Вставленный пораньше")

    got = await db.fetchall("SELECT 1 FROM user_steps WHERE step_id = ? AND user_id = 1", (mid_id,))
    assert got != []  # skipped — не «дальше», шаг всё равно ставится
```

- [ ] **Step 2: Запустить, убедиться что все пять падают** (метода `backfill_step`/вызова из `add_step`
  ещё нет — новые шаги сейчас никому не бэкфиллятся).

Run: `./.venv-agent/bin/python -m pytest -q tests/test_funnel.py -k backfill -v`
Expected: FAIL (5 failed)

- [ ] **Step 3: Добавить `FunnelRepo.backfill_step` и вызвать её из `add_step`:**

```python
async def backfill_step(self, step_id: int, position: int, delay_seconds: int) -> int:
    """Ставит уже существующим активным пользователям только что созданный/включённый шаг —
    по их личной дате получения материала, минуя тех, кто уже получил более поздний шаг."""
    cur = await self.db.conn.execute(
        "INSERT OR IGNORE INTO user_steps(user_id, step_id, due_at, status) "
        "SELECT u.tg_id, ?, u.material_sent_at + ?, 'pending' FROM users u "
        "WHERE u.material_sent_at IS NOT NULL AND u.status = 'active' "
        "AND NOT EXISTS (SELECT 1 FROM user_steps us JOIN funnel_steps fs ON fs.id = us.step_id "
        "WHERE us.user_id = u.tg_id AND fs.position > ? AND us.status = 'sent')",
        (step_id, int(delay_seconds), int(position)),
    )
    await self.db.conn.commit()
    return cur.rowcount or 0
```

В `add_step` — сохранить `step_id = await self.db.execute(...)` в переменную вместо прямого `return`,
вызвать `await self.backfill_step(step_id, position, int(delay_seconds))`, затем `return step_id`.

- [ ] **Step 4: Тесты проходят.**

Run: `./.venv-agent/bin/python -m pytest -q tests/test_funnel.py -k backfill -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Полный прогон.**

Run: `./.venv-agent/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add bot/repo/funnel.py tests/test_funnel.py
git commit -m "feat: новый шаг прогрева сразу встаёт в очередь уже идущим пользователям (backfill_step)"
```

---

## Task 2: Бэкфилл при включении ранее выключенного шага

**Files:**
- Modify: `bot/repo/funnel.py:47-57` (`update_step`)
- Test: `tests/test_funnel.py`

**Interfaces:**
- Consumes: `FunnelRepo.backfill_step` (Task 1), `FunnelRepo.get_step(step_id)` (уже существует)
- Produces: ничего нового наружу — поведение `update_step` для вызывающего кода не меняется

- [ ] **Step 1: Написать тесты:**

```python
async def test_enabling_disabled_step_triggers_backfill(db):
    users, funnel = UsersRepo(db), FunnelRepo(db)
    await users.upsert(1)
    await db.execute("UPDATE users SET material_sent_at = 1000 WHERE tg_id = 1")
    step_id = await funnel.add_step(delay_seconds=60, text="Шаг", enabled=False) \
        if False else await funnel.add_step(delay_seconds=60, text="Шаг")
    await funnel.update_step(step_id, enabled=0)
    await db.execute("DELETE FROM user_steps WHERE step_id = ?", (step_id,))  # имитируем «шаг был выключен ещё до всех»

    await funnel.update_step(step_id, enabled=1)

    rows = await db.fetchall("SELECT 1 FROM user_steps WHERE step_id = ? AND user_id = 1", (step_id,))
    assert rows != []


async def test_disabling_step_does_not_backfill(db):
    users, funnel = UsersRepo(db), FunnelRepo(db)
    await users.upsert(1)
    await db.execute("UPDATE users SET material_sent_at = 1000 WHERE tg_id = 1")
    step_id = await funnel.add_step(delay_seconds=60, text="Шаг")
    await db.execute("DELETE FROM user_steps WHERE step_id = ?", (step_id,))

    await funnel.update_step(step_id, enabled=0)

    assert await db.fetchall("SELECT 1 FROM user_steps WHERE step_id = ?", (step_id,)) == []


async def test_editing_other_fields_does_not_backfill(db):
    users, funnel = UsersRepo(db), FunnelRepo(db)
    await users.upsert(1)
    await db.execute("UPDATE users SET material_sent_at = 1000 WHERE tg_id = 1")
    step_id = await funnel.add_step(delay_seconds=60, text="Шаг")
    await db.execute("DELETE FROM user_steps WHERE step_id = ?", (step_id,))

    await funnel.update_step(step_id, text="Другой текст", delay_seconds=120)

    assert await db.fetchall("SELECT 1 FROM user_steps WHERE step_id = ?", (step_id,)) == []


async def test_reenabling_already_enabled_step_is_noop_for_backfill(db):
    users, funnel = UsersRepo(db), FunnelRepo(db)
    await users.upsert(1)
    await db.execute("UPDATE users SET material_sent_at = 1000 WHERE tg_id = 1")
    step_id = await funnel.add_step(delay_seconds=60, text="Шаг")
    before = len(await db.fetchall("SELECT 1 FROM user_steps WHERE step_id = ?", (step_id,)))

    await funnel.update_step(step_id, enabled=1)  # уже включён

    after = len(await db.fetchall("SELECT 1 FROM user_steps WHERE step_id = ?", (step_id,)))
    assert after == before
```

(Первый тест: убрать неиспользуемый `if False else` — это опечатка-заглушка, реализатор пишет
подготовку данных одним прямым вызовом `add_step` без параметра `enabled`, которого у неё нет; сначала
`update_step(step_id, enabled=0)`, затем чистит `user_steps`, затем включает обратно.)

- [ ] **Step 2: Запустить, убедиться что первый тест падает, остальные три уже проходят «случайно»**
  (текущий `update_step` ничего не бэкфиллит, значит «не делать бэкфилл» тесты пройдут и без изменений —
  это ожидаемо и не проблема, важен только первый).

Run: `./.venv-agent/bin/python -m pytest -q tests/test_funnel.py -k "backfill or noop or does_not_backfill" -v`
Expected: `test_enabling_disabled_step_triggers_backfill` FAIL, остальные PASS

- [ ] **Step 3: Обновить `update_step`:**

```python
async def update_step(self, step_id: int, **fields) -> None:
    allowed = {...}  # без изменений
    before = await self.get_step(step_id) if "enabled" in fields else None
    sets, params = [], []
    for key, value in fields.items():
        if key in allowed:
            sets.append(f"{key} = ?")
            params.append(value)
    if not sets:
        return
    params.append(step_id)
    await self.db.execute(f"UPDATE funnel_steps SET {', '.join(sets)} WHERE id = ?", params)
    if before is not None and int(before["enabled"]) == 0 and int(fields["enabled"]) == 1:
        await self.backfill_step(step_id, before["position"], before["delay_seconds"])
```

- [ ] **Step 4: Все тесты проходят.**

Run: `./.venv-agent/bin/python -m pytest -q tests/test_funnel.py -v`
Expected: PASS

- [ ] **Step 5: Полный прогон + обновить `CLAUDE.md`** (короткая строка в раздел о воронке: новый/
  включённый шаг сам достаётся уже идущим пользователям, ссылка на спеку).

Run: `./.venv-agent/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add bot/repo/funnel.py tests/test_funnel.py CLAUDE.md
git commit -m "feat: включение ранее выключенного шага тоже запускает бэкфилл уже идущим пользователям"
```
