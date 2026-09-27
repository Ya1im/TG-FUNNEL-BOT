# Доступ заказчиков к админке (роль «admin») Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or implement natively task-by-task with test-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** дать выбранным заказчикам доступ ко всей админке кроме опасных/технических действий, через ту же
таблицу приглашений, что уже используется для доступа «только статистика».

**Architecture:** таблица `viewers`/`viewer_invites` получает колонку `role` (`stats`|`admin`);
`ViewersRepo` переименовывается в `AccessRepo`; фильтр всей админки пускает владельца (`.env`) или роль
`admin`; четыре экрана (настройки, подписка, статистика, карточка рассылки) прячут владельческие кнопки
и их обработчики отдельно проверяют `deps.config.is_admin(...)` перед действием.

**Tech Stack:** Python 3.11, aiogram 3.15, aiosqlite, pytest (`./.venv-agent/bin/python -m pytest -q`).

**Spec:** `docs/superpowers/specs/2026-09-27-client-admin-access-design.md`

## Global Constraints

- Роли: только `"stats"` и `"admin"` (см. спека §2). Владелец (`ADMIN_IDS`) не хранится в БД, всегда
  проходит любые проверки.
- Таблицы БД `viewers`/`viewer_invites` не переименовываются — переименовывается только код (спека §3).
- Опасные для роли `admin`: `a:acc*` (управление доступами), `a:set:channel`/`a:set:private`/
  `a:set:force` (канал), `a:set:report`/`a:stat:chat` (чат отчётов), `a:stat:reset*` (обнуление),
  `a:bc:del*`/`a:bc:delok*` (удаление рассылки из истории) — спека §2, §4.
- Свободно для роли `admin`: все добавления/правки/удаления/перестановки блоков материала, шагов
  прогрева, блоков повторного /start, черновика рассылки; вся статистика (просмотр, скачать таблицу,
  переслать эксперту); медиатека; тексты и кнопки (кроме смены самого канала).
- Каждая старая команда `pytest -q` должна проходить после каждой задачи — правок без регрессии.

## Review Focus

- Заказчик с ролью `admin` вручную отправляет старый/чужой `callback_data` владельческого действия
  (например, `a:stat:reset`) — обработчик должен вежливо отказать, а не выполнить действие.
- Пользователь с ролью `stats` (не `admin`) пытается открыть `/admin` — доступ остаётся закрытым, как
  и раньше (регрессия базового `AdminFilter`).
- Старая база без колонки `role` (уже развёрнутый прод) — миграция не должна терять существующих
  зрителей и должна проставить им `role='stats'`.
- Экран «Проверка подписки»: у роли `admin` должны быть скрыты кнопки смены канала, но кнопка правки
  текстов подписки должна остаться — легко перепутать местами.
- У роли `admin` доступ отозван (владелец нажал 🗑 в «Доступе») — при следующем нажатии в открытой
  админке действие должно быть отклонено немедленно (проверка не кэшируется, читается из БД каждый раз).

---

## Task 1: Переименовать `ViewersRepo` → `AccessRepo`, добавить роль (без нового поведения)

Чистый рефакторинг: код и тесты обновляются на новые имена, роль везде `"stats"` по умолчанию —
поведение бота не меняется, весь набор тестов должен пройти как раньше.

**Files:**
- Modify: `bot/schema.sql` (в `CREATE TABLE viewers` и `CREATE TABLE viewer_invites` добавить
  `role TEXT NOT NULL DEFAULT 'stats'`)
- Modify: `bot/db.py` (`_migrate()` — по образцу уже существующих миграций `on_unsub`/`sub_mode`:
  `_has_column("viewers", "role")` → `ALTER TABLE viewers ADD COLUMN role TEXT NOT NULL DEFAULT 'stats'`;
  то же для `viewer_invites`)
- Create: `bot/repo/access.py` (перенести содержимое `bot/repo/viewers.py`, класс `ViewersRepo` →
  `AccessRepo`)
- Delete: `bot/repo/viewers.py`
- Modify: `bot/deps.py` (импорт `from bot.repo.access import AccessRepo`; поле `access: AccessRepo = None`
  вместо `viewers`; в `build()` — `access=AccessRepo(db)`)
- Modify: `bot/handlers/viewer.py:23` (`deps.viewers.is_viewer` → `await deps.access.role(user.id) is not None`)
- Modify: `bot/handlers/user.py:32,93` (`deps.viewers.redeem` → `deps.access.redeem`;
  `deps.viewers.is_viewer` → `await deps.access.role(...) is not None`)
- Modify: `bot/handlers/admin/access.py:24,43,93,111` (`deps.viewers` → `deps.access`)
- Create: `tests/test_access.py` (перенести и переименовать содержимое `tests/test_viewers.py`,
  `ViewersRepo` → `AccessRepo`)
- Delete: `tests/test_viewers.py`
- Modify: `tests/test_e2e.py:1019,1033,1057,1063,1071,1078` (`deps.viewers` → `deps.access`)
- Modify: `tests/test_db.py` (добавить проверку миграции колонки `role`)

**Interfaces:**
- Produces (используется всеми следующими задачами):
  - `AccessRepo.add(self, tg_id: int, name: str | None = None, username: str | None = None, role: str = "stats") -> None`
  - `AccessRepo.remove(self, tg_id: int) -> None`
  - `AccessRepo.role(self, tg_id: int) -> str | None` — `None`, если доступа нет, иначе `"stats"` или `"admin"`
  - `AccessRepo.list(self) -> list[Row]` (строки содержат колонку `role`)
  - `AccessRepo.create_invite(self, created_by: int | None = None, role: str = "stats", now: int | None = None) -> str`
  - `AccessRepo.redeem(self, token: str, tg_id: int, name: str | None, username: str | None, now: int | None = None) -> bool`
    (читает роль приглашения и передаёт её в `add`)

- [ ] **Step 1: Обновить `bot/schema.sql`** — добавить `role TEXT NOT NULL DEFAULT 'stats'` в обе таблицы.

- [ ] **Step 2: Написать тест миграции в `tests/test_db.py`**

```python
async def test_old_viewers_table_gets_role_column(tmp_path):
    # аналогично test_old_database_gets_new_columns_and_keeps_gated_behaviour:
    # создать sqlite-файл со старой схемой viewers (без role), вставить строку,
    # открыть через Database(...).connect(), проверить role == 'stats'
```

- [ ] **Step 3: Запустить тест, убедиться что падает** (`_migrate` ещё не трогает `viewers`).

Run: `./.venv-agent/bin/python -m pytest -q tests/test_db.py -k role -v`
Expected: FAIL

- [ ] **Step 4: Добавить миграцию в `bot/db.py::_migrate`** — тот же паттерн, что для
  `funnel_steps.on_unsub` (`_has_column` + `ALTER TABLE ... ADD COLUMN role TEXT NOT NULL DEFAULT 'stats'`)
  для `viewers` и `viewer_invites`.

- [ ] **Step 5: Тест миграции проходит.**

Run: `./.venv-agent/bin/python -m pytest -q tests/test_db.py -k role -v`
Expected: PASS

- [ ] **Step 6: Создать `bot/repo/access.py`** — перенести `ViewersRepo` из `bot/repo/viewers.py`,
  переименовать класс в `AccessRepo`; `add()` и `create_invite()` получают параметр `role: str = "stats"`
  и пишут его в INSERT; добавить метод `role(self, tg_id) -> str | None`
  (`SELECT role FROM viewers WHERE tg_id = ?`, вернуть значение или `None`); `redeem()` при успехе читает
  `role` приглашения (`SELECT ... role FROM viewer_invites WHERE token = ?`) и передаёт в `add(..., role=invite_role)`.
  Удалить `bot/repo/viewers.py`.

- [ ] **Step 7: Переименовать `tests/test_viewers.py` → `tests/test_access.py`**, заменить импорт и все
  обращения `ViewersRepo`/`is_viewer` на `AccessRepo`/`role(...) is not None`; добавить тест на новый
  параметр:

```python
async def test_add_and_invite_default_to_stats_role(db):
    repo = AccessRepo(db)
    await repo.add(5)
    assert await repo.role(5) == "stats"

async def test_invite_can_carry_admin_role(db):
    repo = AccessRepo(db)
    token = await repo.create_invite(created_by=99, role="admin")
    assert await repo.redeem(token, 7, "Борис", None) is True
    assert await repo.role(7) == "admin"
```

- [ ] **Step 8: Обновить `bot/deps.py`, `bot/handlers/viewer.py`, `bot/handlers/user.py`,
  `bot/handlers/admin/access.py`, `tests/test_e2e.py`** — заменить все обращения `deps.viewers` на
  `deps.access`, `is_viewer(x)` на `(await access.role(x)) is not None`, по местам из списка Files выше.

- [ ] **Step 9: Прогнать весь набор тестов — поведение не должно измениться.**

Run: `./.venv-agent/bin/python -m pytest -q`
Expected: PASS (все тесты, включая новые из Step 2 и Step 7)

- [ ] **Step 10: Commit**

```bash
git add bot/schema.sql bot/db.py bot/repo/access.py bot/deps.py bot/handlers/viewer.py \
        bot/handlers/user.py bot/handlers/admin/access.py tests/test_access.py \
        tests/test_e2e.py tests/test_db.py
git rm bot/repo/viewers.py tests/test_viewers.py
git commit -m "refactor: ViewersRepo -> AccessRepo, добавить роль (stats|admin) без изменения поведения"
```

---

## Task 2: Роль `admin` открывает `/admin`; выбор роли при приглашении

**Files:**
- Modify: `bot/handlers/admin/common.py:39-42` (`AdminFilter`)
- Modify: `bot/handlers/admin/access.py` (экран приглашения — выбор роли)
- Test: `tests/test_e2e.py` (новые проверки в блоке «доступ «только статистика»»/структура админки)

**Interfaces:**
- Consumes: `AccessRepo.role`, `AccessRepo.create_invite(..., role=...)`, `AccessRepo.add(..., role=...)` (Task 1)
- Produces: `AdminFilter` теперь пускает роль `admin`; используется всеми последующими задачами и уже
  существующими роутерами админки без изменений в них самих.

- [ ] **Step 1: Написать тест** в `tests/test_e2e.py`:

```python
async def test_admin_role_can_open_admin_panel_stats_role_cannot(stack):
    dp, bot, session, deps = stack
    await deps.access.add(VIEWER_ID, "Клиент", None, role="admin")
    session.requests.clear()
    await feed(dp, bot, message=make_message("/admin", user_id=VIEWER_ID))
    assert "EditMessageText" in session.names() or "SendMessage" in session.names()

    await deps.access.add(600, "Просто зритель", None, role="stats")
    session.requests.clear()
    await feed(dp, bot, message=make_message("/admin", user_id=600))
    assert session.names() == []
```

- [ ] **Step 2: Убедиться что тест падает** (роль `admin` пока не пускает `AdminFilter`).

Run: `./.venv-agent/bin/python -m pytest -q tests/test_e2e.py -k admin_role_can_open -v`
Expected: FAIL

- [ ] **Step 3: Обновить `AdminFilter.__call__`** в `bot/handlers/admin/common.py`:

```python
return bool(user and deps and (
    deps.config.is_admin(user.id) or await deps.access.role(user.id) == "admin"
))
```

- [ ] **Step 4: Тест проходит.**

Run: `./.venv-agent/bin/python -m pytest -q tests/test_e2e.py -k admin_role_can_open -v`
Expected: PASS

- [ ] **Step 5: Написать тест на выбор роли при приглашении** в `tests/test_e2e.py`:

```python
async def test_owner_picks_role_when_creating_invite(stack):
    dp, bot, session, deps = stack
    text, markup = await _open(dp, bot, session, "a:acc:link")
    assert "a:acc:link:stats" in _callbacks(markup) and "a:acc:link:admin" in _callbacks(markup)

    text, _ = await _open(dp, bot, session, "a:acc:link:admin")
    token = text.split("?start=v_")[1].split("<")[0].split()[0].strip()
    await feed(dp, bot, message=make_message(f"/start v_{token}", user_id=701))
    assert await deps.access.role(701) == "admin"
```

- [ ] **Step 6: Тест падает** (кнопки выбора роли ещё нет).

Run: `./.venv-agent/bin/python -m pytest -q tests/test_e2e.py -k owner_picks_role -v`
Expected: FAIL

- [ ] **Step 7: Переделать `cb_link` в `bot/handlers/admin/access.py`** — вместо немедленного создания
  ссылки сперва показать выбор `[("📊 Только статистика", "a:acc:link:stats"), ("🛠 Полный доступ", "a:acc:link:admin")]`;
  добавить хендлер на `a:acc:link:{role}`, который вызывает `create_invite(created_by=..., role=role)` и
  показывает готовую ссылку. Экран `access_screen` — рядом с каждым в списке показывать роль
  (`"🛠 admin" if v["role"] == "admin" else "📊 stats"`). Экран «➕ Добавить по ID» (`a:acc:add`) —
  аналогично: сначала выбор роли (`a:acc:add:stats`/`a:acc:add:admin`), роль кладём в FSM (`state.update_data(role=...)`),
  `on_add_value` передаёт её в `deps.access.add(..., role=data["role"])`.

- [ ] **Step 8: Тесты проходят.**

Run: `./.venv-agent/bin/python -m pytest -q tests/test_e2e.py -k "owner_picks_role or admin_role_can_open" -v`
Expected: PASS

- [ ] **Step 9: Полный прогон.**

Run: `./.venv-agent/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add bot/handlers/admin/common.py bot/handlers/admin/access.py tests/test_e2e.py
git commit -m "feat: роль admin открывает всю админку, выбор роли при приглашении"
```

---

## Task 3: Владельческие экраны и действия недоступны роли `admin`

**Files:**
- Modify: `bot/handlers/admin/common.py` (добавить `async def require_owner(call, deps) -> bool`)
- Modify: `bot/handlers/admin/settings.py` (`settings_screen`, обработчики `a:set:channel`, `a:set:private`,
  `a:set:force`, `a:set:report`)
- Modify: `bot/handlers/admin/flow.py` (`subscription_screen`)
- Modify: `bot/handlers/admin/stats.py` (`stats_screen`, обработчики `a:stat:chat`, `a:stat:reset`,
  `a:stat:reset:go`)
- Modify: `bot/handlers/admin/broadcast.py` (`broadcast_card`, обработчики `a:bc:del`, `a:bc:delok`)
- Test: `tests/test_e2e.py`

**Interfaces:**
- Consumes: `deps.config.is_admin(user_id)` (существует), `AdminFilter`/роль `admin` уже пускают эти
  роутеры (Task 2).
- Produces: `require_owner(call, deps) -> bool` — переиспользуется во всех обработчиках ниже; `True`,
  если можно продолжать, иначе сам показывает `call.answer("Доступно только владельцу бота", show_alert=True)`
  и возвращает `False`.

- [ ] **Step 1: Написать тесты** (по одному на каждый из 4 экранов + прямой вызов callback) в
  `tests/test_e2e.py`, например:

```python
async def test_content_admin_does_not_see_or_reach_owner_actions(stack):
    dp, bot, session, deps = stack
    await deps.access.add(VIEWER_ID, "Клиент", None, role="admin")

    text, markup = await _open(dp, bot, session, "a:set")  # от имени VIEWER_ID
    assert "a:acc" not in _callbacks(markup) and "a:set:report" not in _callbacks(markup)

    text, markup = await _open(dp, bot, session, "a:flow:sub")
    assert "a:set:channel" not in _callbacks(markup) and "a:set:private" not in _callbacks(markup)
    assert any(cb.startswith("a:set:texts:sub") for cb in _callbacks(markup))

    text, markup = await _open(dp, bot, session, "a:stat")
    assert "a:stat:reset" not in _callbacks(markup) and "a:stat:chat" not in _callbacks(markup)

    session.requests.clear()
    await feed(dp, bot, callback=make_callback("a:stat:reset:go", user_id=VIEWER_ID))
    assert (await deps.users.stats())["total"] >= 0  # ничего не обнулилось
    answer = session.calls("AnswerCallbackQuery")[0]
    assert answer.show_alert is True
```

(`_open`/`_callbacks` — уже существующие в файле хелперы; вызвать их с `user_id=VIEWER_ID` вместо
`ADMIN_ID`, как в остальных тестах доступа.)

- [ ] **Step 2: Тесты падают** (экраны пока не знают о роли).

Run: `./.venv-agent/bin/python -m pytest -q tests/test_e2e.py -k content_admin_does_not_see -v`
Expected: FAIL

- [ ] **Step 3: Добавить `require_owner` в `bot/handlers/admin/common.py`:**

```python
async def require_owner(call: CallbackQuery, deps) -> bool:
    if deps.config.is_admin(call.from_user.id):
        return True
    await call.answer("Доступно только владельцу бота", show_alert=True)
    return False
```

- [ ] **Step 4: В начало каждого обработчика из списка Files** (`a:acc*` — весь роутер `access.py`,
  `a:set:channel`, `a:set:private`, `a:set:force`, `a:set:report`, `a:stat:chat`, `a:stat:reset`,
  `a:stat:reset:go`, `a:bc:del`, `a:bc:delok`) добавить `if not await require_owner(call, deps): return`.
  Для роутера `access.py` — раз он весь владельческий, добавить фильтр на уровне роутера:
  `access.router.callback_query.filter(lambda event, deps: deps.config.is_admin(event.from_user.id))`
  (или отдельный класс `OwnerFilter(BaseFilter)` в `common.py`, если инлайн-лямбда неудобна в aiogram —
  выбрать то, что проще протестировать).

- [ ] **Step 5: В `settings_screen`, `subscription_screen`, `stats_screen`, `broadcast_card`** добавить
  параметр `is_owner: bool` и рисовать владельческие кнопки только при `is_owner=True`; на каждом месте
  вызова этих функций вычислять `is_owner = deps.config.is_admin(<id зовущего>)` (id берётся из
  `call.from_user.id` / `message.from_user.id`, в `MenuRef`-сценариях — из уже доступного в хендлере
  `message.from_user.id` до вызова `show()`).

- [ ] **Step 6: Тесты проходят.**

Run: `./.venv-agent/bin/python -m pytest -q tests/test_e2e.py -k content_admin_does_not_see -v`
Expected: PASS

- [ ] **Step 7: Полный прогон + обновить `CLAUDE.md`** (раздел про доступ «только статистика» дополнить
  ролью `admin`, переименованием `AccessRepo`, списком владельческих исключений).

Run: `./.venv-agent/bin/python -m pytest -q`
Expected: PASS (все тесты, включая Review Focus сценарии — они уже покрыты Task 1–3)

- [ ] **Step 8: Commit**

```bash
git add bot/handlers/admin/common.py bot/handlers/admin/settings.py bot/handlers/admin/flow.py \
        bot/handlers/admin/stats.py bot/handlers/admin/broadcast.py bot/handlers/admin/access.py \
        tests/test_e2e.py CLAUDE.md
git commit -m "feat: владельческие действия и экраны недоступны роли admin (кнопки + проверка в хендлере)"
```
