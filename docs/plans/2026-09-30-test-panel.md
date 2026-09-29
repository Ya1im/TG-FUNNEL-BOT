# Панель /test — план

Спека: docs/superpowers/specs/2026-09-30-test-panel-design.md. Натив, TDD.

1. `bot/preprod.py`: `tester_ids`, `allowed_user_ids(db=...)`; вызовы в scheduler/broadcast/services передают db.
2. `FunnelRepo`: `set_fast`, `skip_wait`, `user_progress`, `has_fast_pending`; `Scheduler.run_user`, адаптивный сон.
3. `bot/tester.py` (текст панели, `is_tester`) и `bot/handlers/tester.py` (/test, `t:*`), кнопка в меню и Прогреве, команды.
4. Права: preprod/recall для роли admin, release — владелец.
5. Тесты, инструкция клиента (md+PDF), CLAUDE.md, ревью.
