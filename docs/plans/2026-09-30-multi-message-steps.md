# Несколько сообщений в шаге прогрева — план

Spec: `docs/superpowers/specs/2026-09-30-multi-message-steps-design.md`. Исполнение: нативно, TDD.

## Task 1: данные и отправка
Files: `bot/schema.sql`, `bot/db.py` (`_migrate`), `bot/repo/funnel.py`, `bot/scheduler.py`, `bot/recall.py`,
tests `tests/test_multi_steps.py`.
- Колонка `extra_messages_json`; `step_messages`, `blocks_from_row`, `set_messages`, `add_step(extra_messages)`,
  выдача колонки в `list_steps`/`due_steps`; export/import.
- Планировщик шлёт все блоки с паузой, собирает `message_ids`, политика частичной доставки.
- `recall` считает все сообщения шага.
- Тесты: порядок и пауза; ids всех сообщений в журнале; сбой на 2-м сообщении → шаг sent, без дублей;
  блокировка на 2-м → очередь очищена; сбой на 1-м → повтор; старая БД мигрирует; export/import round-trip;
  set_messages с пустым списком → ValueError; recall legacy суммирует.

## Task 2: админка
Files: `bot/handlers/admin/common.py` (FSM `FunnelMsgAdd`, `FunnelMsgEdit`, `FunnelAdd.collecting`),
`bot/handlers/admin/funnel.py`, tests в `tests/test_e2e.py`.
- Экраны и callback из спецификации; создание шага из нескольких сообщений; кнопки/правка/порядок/удаление.
- Тесты: создание шага из 3 сообщений с фото; добавление в существующий; кнопки только у 2-го;
  порядок; удаление; последнее не удаляется; «Показать» шлёт все.

## Task 3: документация и ревью
`client-guide.md` + PDF, `CLAUDE.md`, независимый ревью через Agent, коммит.
