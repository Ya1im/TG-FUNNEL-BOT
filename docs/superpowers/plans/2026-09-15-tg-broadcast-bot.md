# План реализации: телеграм-бот рассылок с воронкой и прогревом

> Спека: docs/superpowers/specs/2026-09-15-tg-broadcast-bot-design.md

**Цель:** бот, который на `/start` отдаёт кружок и меню с проверкой подписки, после подтверждения выдаёт материал, затем ведёт цепочку прогрева, и позволяет владельцу слать массовые рассылки любым типом контента.

**Архитектура:** один asyncio-процесс: aiogram 3 (long polling) + SQLite (aiosqlite, WAL) + внутренний планировщик с тиком раз в 60 секунд. Весь контент хранится как `file_id` в медиатеке, тексты и шаги воронки — в БД, правятся из админки.

**Стек:** Python 3.11, aiogram 3.15, aiosqlite, pydantic-settings, pytest + pytest-asyncio, Docker.

## Файловая структура

```
bot/
  __main__.py        точка входа: конфиг → бот → роутеры → планировщик → polling
  config.py          .env → Config (токен, админы, путь к БД, скорость отправки)
  db.py              подключение, WAL, применение schema.sql, хелперы fetch/execute
  schema.sql         таблицы users, media, funnel_steps, user_steps, broadcasts,
                     broadcast_targets, settings, material_blocks
  repo/users.py      upsert, статусы, сегменты, статистика, CSV
  repo/media.py      CRUD медиатеки
  repo/funnel.py     CRUD шагов, enqueue воронки, выборка due, отметки статусов
  repo/broadcasts.py черновики, таргеты, прогресс, статистика
  repo/settings.py   key-value с дефолтами
  content.py         ContentBlock (текст + медиа + кнопки) → отправка одним вызовом
  sender.py          RateLimiter, safe_send: 429 → retry_after, 403 → blocked
  subscription.py    check_subscription(bot, channel_id, user_id) + кэш в users
  scheduler.py       тик: due-шаги воронки + запланированные рассылки + бэкап БД
  broadcast.py       движок рассылки: батчи, прогресс, отчёт, докатывание после рестарта
  keyboards.py       инлайн-клавиатуры пользователя и админки
  texts.py           дефолтные тексты (фоллбэк, если в настройках пусто)
  middlewares.py     регистрация пользователя, фильтр админа
  handlers/user.py   /start, check_sub, /reset, выдача материала
  handlers/admin/*   menu, media, funnel, broadcast, settings, stats
tests/               pytest на логику очереди, гейта подписки, sender, рассылки
```

## Задачи

### Задача 1: каркас, конфиг, БД
**Файлы:** `requirements.txt`, `.env.example`, `.gitignore`, `bot/config.py`, `bot/db.py`, `bot/schema.sql`, `tests/conftest.py`, `tests/test_db.py`

- [ ] Тест: `test_schema_applies` — открыть БД в tmp, применить схему, проверить, что все 8 таблиц существуют. Ожидание до реализации: FAIL (нет модуля `bot.db`).
- [ ] Тест: `test_settings_defaults` — чтение несуществующего ключа возвращает дефолт.
- [ ] Реализация: `Config` читает `BOT_TOKEN`, `ADMIN_IDS`, `DB_PATH`, `MESSAGES_PER_SECOND`, `TICK_SECONDS`. `Database.connect()` включает `PRAGMA journal_mode=WAL`, `busy_timeout=5000`, применяет `schema.sql` идемпотентно (`CREATE TABLE IF NOT EXISTS`).
- [ ] Запуск: `pytest tests/test_db.py -v` → PASS.
- [ ] Коммит: `feat: каркас проекта, конфиг и схема БД`

### Задача 2: репозитории users / settings / media
**Файлы:** `bot/repo/users.py`, `bot/repo/settings.py`, `bot/repo/media.py`, `tests/test_repo.py`

- [ ] Тест: `test_upsert_user_keeps_source` — повторный `/start` без payload не затирает первоначальный источник.
- [ ] Тест: `test_segment_counts` — сегменты `all / subscribed / not_subscribed / got_material / blocked` считают правильные множества.
- [ ] Тест: `test_media_slug_unique` — повторный slug заменяет запись, а не плодит дубли.
- [ ] Реализация репозиториев (async, через `Database`).
- [ ] Запуск: `pytest tests/test_repo.py -v` → PASS.
- [ ] Коммит: `feat: репозитории пользователей, настроек и медиатеки`

### Задача 3: контент и отправка
**Файлы:** `bot/content.py`, `bot/sender.py`, `tests/test_sender.py`

- [ ] Тест: `test_rate_limiter_respects_speed` — 40 вызовов при лимите 20/сек занимают ≥ 1.9 сек (с ускоренным `monotonic`).
- [ ] Тест: `test_safe_send_marks_blocked` — `TelegramForbiddenError` → пользователь получает статус `blocked`, исключение не всплывает.
- [ ] Тест: `test_safe_send_retries_after_429` — `TelegramRetryAfter(retry_after=1)` → ждём и повторяем один раз, второй вызов успешен.
- [ ] Реализация: `ContentBlock.send(bot, chat_id)` выбирает метод по `kind` (`send_photo/send_video/send_video_note/send_document/send_audio/send_voice/send_animation/send_message`), прикладывает клавиатуру из `buttons_json`. `safe_send(...)` оборачивает любой вызов.
- [ ] Запуск: `pytest tests/test_sender.py -v` → PASS.
- [ ] Коммит: `feat: единая отправка контента с лимитером и обработкой ошибок`

### Задача 4: проверка подписки
**Файлы:** `bot/subscription.py`, `tests/test_subscription.py`

- [ ] Тест: `test_statuses` — `member/administrator/creator` → True; `left/kicked` → False; `restricted` с `is_member=True` → True, с False → False.
- [ ] Тест: `test_check_updates_cache` — результат пишется в `users.is_subscribed` и `sub_checked_at`.
- [ ] Тест: `test_channel_not_configured` — если канал не задан, функция возвращает True (гейт не блокирует воронку на пустой настройке).
- [ ] Реализация через `bot.get_chat_member`, ошибки API → False + лог.
- [ ] Запуск: `pytest tests/test_subscription.py -v` → PASS.
- [ ] Коммит: `feat: проверка подписки на канал`

### Задача 5: воронка — хранение и очередь
**Файлы:** `bot/repo/funnel.py`, `tests/test_funnel.py`

- [ ] Тест: `test_enqueue_creates_all_steps` — вход в воронку ставит все включённые шаги с `due_at = now + delay`.
- [ ] Тест: `test_enqueue_is_idempotent` — повторный вызов не создаёт дублей (уникальный индекс `user_id, step_id`).
- [ ] Тест: `test_due_selection_boundaries` — выбираются только `pending` с `due_at <= now`; `sent/skipped/held` не выбираются.
- [ ] Тест: `test_fast_mode_enqueue` — тестовый прогон ставит шаги с задержками по 10 секунд, сохраняя порядок.
- [ ] Реализация CRUD шагов + `enqueue_funnel(user_id, fast=False)` + `due_steps(limit)` + `mark_sent/postpone/skip`.
- [ ] Запуск: `pytest tests/test_funnel.py -v` → PASS.
- [ ] Коммит: `feat: хранение воронки и очередь отправок`

### Задача 6: планировщик прогрева
**Файлы:** `bot/scheduler.py`, `tests/test_scheduler.py`

- [ ] Тест: `test_tick_sends_due_step` — шаг с наступившим временем уходит, помечается `sent`.
- [ ] Тест: `test_gated_step_postponed_when_unsubscribed` — шаг с `requires_subscription=1` у отписавшегося: уходит напоминание, `due_at` сдвигается на +6 ч, `attempts=1`, сам шаг остаётся `pending`.
- [ ] Тест: `test_gated_step_skipped_after_three_attempts` — на четвёртом заходе шаг становится `skipped`, напоминание больше не шлётся.
- [ ] Тест: `test_blocked_user_queue_cleared` — при 403 все pending-шаги пользователя становятся `skipped`, юзер `blocked`.
- [ ] Реализация: `tick()` — забрать due пачкой 200, по каждому: гейт → отправка → отметка. Плюс запуск запланированных рассылок и суточный бэкап БД.
- [ ] Запуск: `pytest tests/test_scheduler.py -v` → PASS.
- [ ] Коммит: `feat: планировщик прогрева с гейтом подписки`

### Задача 7: пользовательский сценарий
**Файлы:** `bot/handlers/user.py`, `bot/keyboards.py`, `bot/texts.py`, `bot/middlewares.py`, `tests/test_start_flow.py`

- [ ] Тест: `test_start_registers_and_sends_note` — `/start ig_reels` создаёт пользователя с источником и отправляет кружок, затем меню.
- [ ] Тест: `test_check_sub_success_sends_material_and_enqueues` — успешная проверка → блоки материала отправлены, очередь воронки создана, `material_sent_at` проставлен.
- [ ] Тест: `test_check_sub_failure_keeps_menu` — неуспешная проверка → всплывашка, материал не отправлен, очередь пуста.
- [ ] Реализация: хендлеры `/start`, `check_sub`, `/reset`; выдача материала — последовательность блоков из `material_blocks` (медиа + тексты + кнопки), плюс персональная invite-ссылка, если задан закрытый канал.
- [ ] Запуск: `pytest tests/test_start_flow.py -v` → PASS.
- [ ] Коммит: `feat: сценарий старта, проверки подписки и выдачи материала`

### Задача 8: админка
**Файлы:** `bot/handlers/admin/{__init__,media,funnel,settings,stats}.py`

- [ ] Меню `/admin` с разделами и защитой по `ADMIN_IDS`.
- [ ] Медиатека: загрузка (любой тип → бот просит slug), список, просмотр, удаление.
- [ ] Воронка: список шагов с задержками, добавление (задержка → контент → гейт подписки → кнопки), редактирование, включение/выключение, удаление, «прогнать на себе», экспорт/импорт JSON.
- [ ] Настройки: кружок приветствия, тексты меню и напоминаний, публичный канал (с проверкой, что бот там админ), закрытый канал, блоки материала.
- [ ] Статистика: счётчики, разбивка по источникам, выгрузка CSV.
- [ ] Ручная проверка по чек-листу README.
- [ ] Коммит: `feat: админка — медиатека, воронка, настройки, статистика`

### Задача 9: массовые рассылки
**Файлы:** `bot/broadcast.py`, `bot/handlers/admin/broadcast.py`, `tests/test_broadcast.py`

- [ ] Тест: `test_targets_snapshot_by_segment` — при создании рассылки список получателей фиксируется по сегменту.
- [ ] Тест: `test_broadcast_resumes_after_restart` — часть таргетов уже `sent`, повторный запуск досылает только оставшихся.
- [ ] Тест: `test_blocked_counted_not_failed` — 403 попадает в счётчик «заблокировали», а не в ошибки.
- [ ] Реализация: черновик из присланных админом сообщений (сохраняем `chat_id/message_id` для `copy_message`), предпросмотр, «тест себе», сегмент, запуск или планирование на время, живой прогресс раз в 5 секунд, финальный отчёт.
- [ ] Запуск: `pytest tests/test_broadcast.py -v` → PASS.
- [ ] Коммит: `feat: массовые рассылки с сегментами и докатыванием`

### Задача 10: упаковка и деплой
**Файлы:** `Dockerfile`, `docker-compose.yml`, `README.md`, `Makefile`

- [ ] Dockerfile на python:3.11-slim, non-root пользователь, `CMD python -m bot`.
- [ ] docker-compose: volume `./data:/app/data`, `env_file: .env`, `restart: unless-stopped`, ротация логов.
- [ ] README: настройка @BotFather, права бота в канале, переезд тест → прод, ручной чек-лист админки, бэкапы и восстановление.
- [ ] Проверка: `docker compose config` без ошибок, `pytest` целиком зелёный.
- [ ] Коммит: `chore: docker, деплой и документация`

## Самопроверка плана

- Покрытие спеки: п.4 данных → задачи 1–2, 5, 9; п.5 сценарий → 7; п.6 прогрев → 6; п.7 админка → 8; п.8 надёжность → 3, 9; п.9 тесты → в каждой задаче; п.10 деплой → 10.
- Плейсхолдеров нет: каждая задача имеет файлы, тесты с ожидаемым результатом и команду запуска.
- Имена согласованы: `enqueue_funnel`, `due_steps`, `safe_send`, `ContentBlock`, `check_subscription` используются одинаково во всех задачах.
