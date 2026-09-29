# Воронка с ветвлением по клику и автовыдачей урока — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or implement natively task-by-task with test-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Бот выдаёт урок сам через час после /start, считает клик по кнопке «Смотреть урок» и по нему отменяет пуши и переносит основную цепочку на «клик + 1 ч».

**Architecture:** Абсолютная модель задержек сохраняется. Кнопка с признаком `track` становится callback-кнопкой `lc:<sha1(url)[:16]>`; хендлер фиксирует первый клик (`users.lesson_clicked_at`), вызывает `FunnelRepo.apply_click` (пропуск пушей + сдвиг цепочки одной транзакцией) и присылает обычную URL-кнопку. Автовыдача — хук планировщика поверх атомарного claim `deliver_material_once`.

**Tech Stack:** Python 3.11, aiogram 3.15, aiosqlite, pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-09-29-funnel-click-branching-design.md`

## Global Constraints

- Все тексты для пользователей и админа — на русском. Время — целые unix-секунды (UTC).
- Колонки добавляются через `Database._migrate` (`_has_column` + `ALTER TABLE`) **и** в `bot/schema.sql`.
- Обратная совместимость: кнопка без третьего поля `| клик` остаётся обычной URL-кнопкой; существующие боевые воронки не меняются.
- callback_data ≤ 64 байта; префикс `lc:`.
- Настройки: `auto_deliver_minutes="60"` (0 — выкл), `lesson_link_text="Вот урок 👇"`, `lesson_link_btn="Открыть урок"`.
- `FAST_STEP_SECONDS = 10` — «после клика» в быстром режиме.
- Claim автовыдачи истекает через 600 секунд.
- На функциональные изменения обновляются `docs/client-guide/client-guide.md` + `bot/assets/client-guide.pdf` (коммитятся вместе) и `CLAUDE.md`.
- Роутеры — модульные синглтоны: тесты, собирающие диспетчер, используют `fresh_dispatcher` из `tests/conftest.py`.

## Review Focus

- Клик от пользователя без очереди шагов → пишется время, ошибок нет (тест в Task 2).
- Два клика подряд / повторный клик спустя время → сдвиг применён один раз (Task 2).
- Клик, когда «призыв» уже отправлен или пропущен → цепочка не двигается (Task 2).
- Подписка и тик планировщика одновременно → урок выдан ровно один раз (Task 3).
- Автовыдача пользователю со статусом `blocked`/`stopped` и уже получившему урок → пропуск (Task 3).
- Сдвиг назад: шаги, подтянутые к моменту клика, уходят в очередь с нормальным интервалом, а не пачкой (Task 2, `test_click_reanchors_chain_and_preserves_spacing`).
- Кнопка с `track` в рассылке или «повторном /start» остаётся ссылкой (Task 1).
- Устаревший хеш (админ сменил ссылку) → alert, а не исключение (Task 2).

---

### Task 1: Модель данных, настройки, кнопки с признаком «клик»

**Files:**
- Modify: `bot/schema.sql`, `bot/db.py` (`_migrate`), `bot/repo/settings.py` (`DEFAULTS`), `bot/repo/users.py` (`reset`), `bot/handlers/admin/common.py` (`parse_buttons`, `buttons_hint`), `bot/content.py` (`ContentBlock.keyboard`)
- Test: `tests/test_db.py`, `tests/test_repo.py`, `tests/test_funnel.py` (кнопки — там же, где тесты `ContentBlock`; если их нет — новый `tests/test_tracked_buttons.py`)

**Interfaces:**
- Produces: колонки `users.lesson_clicked_at INTEGER`, `users.deliver_claim_at INTEGER`, `users.funnel_fast INTEGER NOT NULL DEFAULT 0`, `funnel_steps.stop_on_click INTEGER NOT NULL DEFAULT 0`, `funnel_steps.after_click_seconds INTEGER`; настройки из Global Constraints; `bot/content.py::button_hash(url: str) -> str` (sha1 hex, 16 символов); `ContentBlock.track: bool = False` (поле датакласса; `keyboard()` без параметров) — при `track=True` кнопка с `btn.get("track")` строится как `InlineKeyboardButton(text, callback_data=f"lc:{button_hash(url)}")`, иначе URL-кнопка; `parse_buttons` возвращает `{"text","url"}` или `{"text","url","track":True}`.

- [ ] **Step 1: Failing tests**
  - `test_db.py::test_migrate_adds_click_columns` — база, созданная старой схемой (без новых колонок), после `connect()` содержит все 5 колонок (`_has_column`), повторный `connect()` не падает.
  - `test_repo.py::test_reset_clears_click_and_claim` — после `users.reset(id)` `lesson_clicked_at` и `deliver_claim_at` равны `None`, `funnel_fast == 0`.
  - `test_parse_buttons_track`: `"Смотреть урок | https://a.b/x | клик"` → `[{"text":"Смотреть урок","url":"https://a.b/x","track":True}]`; `... | КЛИК` → то же; без третьего поля → без ключа `track`; `"a | https://x | что-то"` → без `track`.
  - `test_keyboard_track_builds_callback`: `keyboard(track=True)` даёт `callback_data == "lc:" + button_hash(url)` и `len(callback_data.encode()) <= 64`; `keyboard()` (по умолчанию) даёт `url=` даже у `track`-кнопки; кнопка без `track` при `track=True` остаётся URL.
  - `test_settings_defaults_click`: `SettingsRepo.get("auto_deliver_minutes") == "60"` и два текста по умолчанию.

- [ ] **Step 2: Run** `pytest tests -q -k "click or track"` — Expected: FAIL.
- [ ] **Step 3: Implement** — схема и миграции по Interfaces (в `_migrate` — четыре независимых `if not _has_column`); `parse_buttons` разбирает третье поле через `split("|")`, `url` берётся из второго, лишние поля игнорируются; `keyboard(track=False)` — параметр по умолчанию `False` сохраняет поведение всех существующих вызовов. `users.reset` дописывает три поля в `UPDATE`. `buttons_hint` показывает « 🎯» у кнопок с `track`.
- [ ] **Step 4: Run** `pytest tests -q` — Expected: всё зелёное (205 + новые).
- [ ] **Step 5: Commit** `feat: колонки клика/claim/fast, настройки автовыдачи, кнопка «| клик»`

---

### Task 2: Клик и ветвление

**Files:**
- Modify: `bot/repo/funnel.py` (`apply_click`, `enqueue` fast-режим, `export_json`/`import_json`, `add_step`/`update_step` принимают новые поля), `bot/repo/users.py` (`mark_lesson_clicked`), `bot/services.py` (`record_click`, `find_tracked_url`), `bot/sender.py` или `bot/repo/funnel.py::block_from_row` (передать `track=True` для шагов и блоков материала), `bot/handlers/user.py` (callback `lc:`), `bot/handlers/admin/funnel.py` (карточка, переключатель, ввод задержки после клика)
- Test: `tests/test_funnel.py`, `tests/test_start_flow.py`, `tests/test_e2e.py`

**Interfaces:**
- Consumes: Task 1 (`button_hash`, колонки, `keyboard(track=True)`).
- Produces:
  - `UsersRepo.mark_lesson_clicked(tg_id: int, now: int) -> bool` — `UPDATE users SET lesson_clicked_at=? WHERE tg_id=? AND lesson_clicked_at IS NULL`; `True`, если строка обновлена.
  - `FunnelRepo.apply_click(user_id: int, click_at: int) -> None` — алгоритм §3.3 спеки в одной транзакции (пуши `stop_on_click` → `skipped`/`'клик по уроку'`; якорь = первый по `position` `pending`-шаг с `after_click_seconds IS NOT NULL`; `delta = click_at + after_click_seconds - anchor.due_at`; `due_at += delta` у всех `pending` с `position >= anchor.position`).
  - `services.record_click(deps, user_id: int, now: int | None = None) -> bool` — `mark_lesson_clicked` + при `True` `funnel.apply_click`; возвращает признак «первый клик».
  - `services.find_tracked_url(deps, digest: str) -> str | None` — перебор `track`-кнопок шагов (`funnel.list_steps()`) и блоков материала (`material.list_blocks()`).
  - `FunnelRepo.enqueue(user_id, fast=False, now=None)` — при `fast=True` дополнительно выставляет `users.funnel_fast = 1`; `apply_click(user_id, click_at, fast=False)` при `fast=True` берёт задержку якоря `FAST_STEP_SECONDS` вместо `after_click_seconds`; `record_click` читает `users.funnel_fast` и передаёт его в `apply_click`.
  - Колбэки админки: `a:fun:stc:<id>` (переключить `stop_on_click`), `a:fun:ack:<id>` (ввод задержки после клика; `-` убирает).

- [ ] **Step 1: Failing tests** (`test_funnel.py`, репозиторий и сервис)
  - `test_click_skips_stop_on_click_steps` — 5 пушей (`stop_on_click=1`) + призыв (`after_click_seconds=3600`) + 2 хвостовых шага; клик → все 5 пушей `skipped` с `last_error='клик по уроку'`.
  - `test_click_reanchors_chain_and_preserves_spacing` — до клика у призыва `due=T0+44ч30м`, хвост `+12ч`, `+24ч`; клик в `T0+10мин` → призыв `T0+10мин+1ч`, интервалы между призывом и хвостом те же (`==12ч`, `==24ч`).
  - `test_click_after_push5_before_anchor_moves_forward_or_back` — клик в `T0+44ч` → призыв на `+1ч` (дельта `+30 м`), хвост сдвинут на ту же дельту.
  - `test_click_after_anchor_sent_no_shift` — якорь `sent` → `due_at` остальных не изменились; пуши уже не `pending`, ничего не ломается.
  - `test_click_after_anchor_skipped_no_shift`.
  - `test_double_click_is_idempotent` — второй `record_click` возвращает `False`, `due_at` не меняются повторно.
  - `test_click_without_queue_only_records_time` — пользователь без `user_steps`: `lesson_clicked_at` записан, исключений нет.
  - `test_click_in_fast_mode` — очередь создана `enqueue(fast=True)`: после клика призыв стоит на `click_at + FAST_STEP_SECONDS` (в `record_click` при `fast` — см. Step 3).
  - `test_export_import_roundtrip_new_fields` — `stop_on_click`, `after_click_seconds` и `track` в кнопке переживают `export_json → import_json`.
  - `test_find_tracked_url` — находит URL по хешу среди шагов и материала; неизвестный хеш → `None`.
  - `test_tracked_button_only_in_steps_and_material` (`test_e2e.py`/`test_wiring.py`): сообщение шага прогрева и блока материала содержит callback-кнопку `lc:`; рассылка и «повторный /start» с той же кнопкой — URL.
  - `test_e2e.py::test_lc_callback_records_click_and_sends_link` — нажатие `lc:<hash>` → `SendMessage` с URL-кнопкой на исходный адрес и текстом `lesson_link_text`; `lesson_clicked_at` записан; повторное нажатие шлёт ссылку снова, но не двигает цепочку.
  - `test_e2e.py::test_lc_callback_unknown_hash_alerts` — `answer(show_alert=True)` с текстом «Ссылка устарела, напишите /start», исключения нет.
  - `test_e2e.py::test_admin_step_card_click_toggles` — карточка показывает «Отменяется после клика»; `a:fun:stc:<id>` переключает флаг; `a:fun:ack:<id>` + ввод `1ч` ставит `after_click_seconds=3600`, `-` сбрасывает.

- [ ] **Step 2: Run** `pytest tests -q -k "click or lc_ or tracked"` — Expected: FAIL.
- [ ] **Step 3: Implement**
  - Быстрый режим: колонка `users.funnel_fast INTEGER NOT NULL DEFAULT 0` (в `schema.sql` и `_migrate`); ставится в `enqueue(fast=True)`, сбрасывается в `users.reset`.
  - `apply_click`: одна транзакция через `db.conn`; выбор якоря и сдвиг — чистый SQL (`UPDATE user_steps SET due_at = due_at + :delta WHERE user_id=? AND status='pending' AND step_id IN (SELECT id FROM funnel_steps WHERE position >= :anchor_pos)`).
  - Хендлер `lc:` в `bot/handlers/user.py`: `F.data.startswith("lc:")`; порядок — `find_tracked_url` → `record_click` → `call.answer()` → отправка `lesson_link_text` с `lesson_link_btn`-кнопкой через `safe_send`.
  - `block_from_row` для шагов/материала возвращает блок, у которого `keyboard()` вызывается с `track=True` (флаг в `ContentBlock`, по умолчанию `False`, ставится только в `block_from_row` шагов и материала).
  - Админка: строки в карточке шага (`🛑 Отменяется после клика`, `🎯 После клика`), кнопки в «⋯ Ещё», состояние ввода по образцу `a:fun:delay`; `add_step`/`update_step`/`export_json`/`import_json` расширены полями.
- [ ] **Step 4: Run** `pytest tests -q` — Expected: PASS.
- [ ] **Step 5: Commit** `feat: клик по уроку и ветвление воронки`

---

### Task 3: Автовыдача урока

**Files:**
- Modify: `bot/services.py` (`deliver_material_once`, правка `check_subscription_flow`, `auto_deliver_due`), `bot/scheduler.py` (`deliver_hook`), `bot/__main__.py` (подключение хука), `bot/repo/users.py` (`claim_delivery`, `due_for_auto_delivery`), `bot/handlers/admin/settings.py` (подпись и поле `auto_deliver_minutes`)
- Test: `tests/test_start_flow.py`, `tests/test_scheduler.py`, `tests/test_subscription.py`, `tests/test_e2e.py`

**Interfaces:**
- Consumes: Task 1 (`deliver_claim_at`, `auto_deliver_minutes`).
- Produces:
  - `UsersRepo.claim_delivery(tg_id: int, now: int, ttl: int = 600) -> bool` — атомарный UPDATE из §3.1 спеки; `True` — занято этим вызовом.
  - `UsersRepo.due_for_auto_delivery(now: int, minutes: int, limit: int = 200) -> list[int]` — `status='active' AND material_sent_at IS NULL AND started_at + minutes*60 <= now`.
  - `services.deliver_material_once(bot, deps, chat_id: int, *, auto: bool = False) -> bool` — claim → `deliver_material(..., send_invite=not auto_unsubscribed)` → при исключении снять claim и пробросить. `deliver_material` получает необязательный параметр `send_invite: bool = True`.
  - `services.auto_deliver_due(bot, deps, now: int | None = None) -> int` — читает `auto_deliver_minutes` (≤0/пусто → 0 и выход), выдаёт урок каждому из `due_for_auto_delivery` через `deliver_material_once(..., auto=True)`; «неподписан» определяется `deps.gate.status(...)` ≠ `"yes"` (ошибка проверки = неподписан, приватную ссылку не шлём).
  - `Scheduler(..., deliver_hook: Callable[[], Awaitable[None]] | None = None)` — вызывается в `tick` до обработки шагов.

- [ ] **Step 1: Failing tests**
  - `test_claim_delivery_is_atomic` — первый вызов `True`, второй `False`; после `ttl+1` секунд снова `True`; для пользователя с `material_sent_at` — `False`.
  - `test_due_for_auto_delivery_filters` — берёт по времени; не берёт `blocked`/`stopped`, получивших урок, «ещё рано».
  - `test_auto_delivery_delivers_after_timeout_without_subscription` — пользователь без подписки через 61 мин получает материал и `enqueue` ставит шаги; через 59 мин — нет.
  - `test_auto_delivery_disabled_when_zero`.
  - `test_auto_delivery_skips_private_invite_for_unsubscribed` — в отправленных сообщениях нет `create_chat_invite_link`/`private_text`; для подписанного пользователя (если урок дошёл через тик) ссылка отправляется.
  - `test_subscription_and_tick_deliver_once` — параллельно (`asyncio.gather`) `check_subscription_flow` и `auto_deliver_due` → материал отправлен ровно один раз, `user_steps` без дублей.
  - `test_deliver_releases_claim_on_error` — `send_block` бросает исключение → `deliver_claim_at IS NULL`, повторная попытка возможна.
  - `test_scheduler_tick_runs_deliver_hook`.
  - `test_admin_settings_has_auto_deliver_minutes` (`test_e2e.py`): поле есть в разделе настроек, значение сохраняется.

- [ ] **Step 2: Run** `pytest tests -q -k "auto_deliver or claim or deliver_once"` — Expected: FAIL.
- [ ] **Step 3: Implement** — `check_subscription_flow` вызывает `deliver_material_once` вместо прямого `deliver_material` (проверка `material_sent_at` остаётся как быстрый выход); `__main__` передаёт в `Scheduler` `deliver_hook=lambda: auto_deliver_due(bot, deps)`; `mark_material_sent` и `enqueue` вызываются как раньше в конце `deliver_material`, claim не заменяет `material_sent_at`.
- [ ] **Step 4: Run** `pytest tests -q` — Expected: PASS.
- [ ] **Step 5: Commit** `feat: автовыдача урока по таймеру для неподписавшихся`

---

### Task 4: Документация и финальная проверка

**Files:**
- Modify: `docs/client-guide/client-guide.md`, `bot/assets/client-guide.pdf` (пересборка в зеркальном дереве `/tmp/repo-mirror/docs/client-guide/`, шрифт DejaVuSans, эмодзи вырезаются), `CLAUDE.md`
- Test: `tests/test_e2e.py::test_guide_*` (существующие проверки PDF должны остаться зелёными)

**Interfaces:**
- Consumes: Tasks 1–3.

- [ ] **Step 1:** В `client-guide.md` добавить: синтаксис `| клик`, переключатели шага («Отменять после клика», «Задержка после клика»), настройку «Автовыдача урока», пояснение «как работает ветвление», запись в журнал изменений (29.09.2026). Раздел про воронку заказчика — таблица из §7 спеки.
- [ ] **Step 2:** Пересобрать PDF (`reportlab`, генератор из прошлой сессии), сверить `pdftotext` — новые разделы на месте, нет «чёрных квадратов».
- [ ] **Step 3:** `CLAUDE.md`: раздел про клики/автовыдачу, новые «грабли» (claim и TTL; клик только по `track`-кнопкам; устаревший хеш после смены ссылки; `move_step` не пересчитывает очередь).
- [ ] **Step 4: Run** `pytest tests -q` — Expected: все зелёные (205 + новые), `python -m compileall bot -q` без ошибок.
- [ ] **Step 5: Commit** `docs: инструкция и CLAUDE.md для кликов и автовыдачи` (код из Tasks 1–3 уже закоммичен своими коммитами).
- [ ] **Step 6: Инструкция для владельца:** `bash deploy.sh` из `~/Desktop/тима`; затем в админке внести воронку по таблице из §7 спеки и проверить «Быстрым тестом» ветку «клик» и ветку «без клика».
