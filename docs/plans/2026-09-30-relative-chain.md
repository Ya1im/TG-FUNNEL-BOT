# Цепочка «от предыдущего поста» — план

Спека: docs/superpowers/specs/2026-09-30-relative-chain-design.md. Исполнение — натив, TDD, один коммит на задачу.

## Задача 1. Убрать учёт клика
Удалить: `bot/content.py` (track, CLICK_PREFIX, button_hash), `bot/handlers/user.py` (`lc:`), `bot/services.py` (find_tracked_url, record_click, повтор клика), `UsersRepo.mark_lesson_clicked/clear_click`, `FunnelRepo.apply_click` + поля stop_on_click/after_click, админ-экраны клика, «| клик» в parse_buttons, тексты lesson_link_*; тесты test_click.py, test_tracked_buttons.py. Проверка: тесты зелёные, grep по `click` пуст (кроме колонок БД).

## Задача 2. Цепочка (ядро)
`FunnelRepo`: NOT_SCHEDULED, `normalize_users`, `enqueue`, `mark_sent/finish` → нормализация, `backfill_step`, `update_step` (enabled/delay/position), `delete_step`, `move_step`, `rebase_overdue_heads`. Тесты: test_chain.py.

## Задача 3. Миграция и запуск
`init_chain` при старте (`chain_initialized`), `rebase_overdue_heads` при выключении предпрода, обновить `_process` в scheduler. Тесты: «10 давних людей → по одному сообщению».

## Задача 4. Пометка предпрода
`bot/preprod.py::timing_note`, отправка в scheduler перед постом, журнал. Тесты.

## Задача 5. Админка и документы
Тексты «через сколько после предыдущего поста», карточка шага, client-guide md/PDF, CLAUDE.md. Ревью независимым агентом, полный прогон тестов, коммит.
