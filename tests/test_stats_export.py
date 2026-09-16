"""Пересборка сводной .xlsx-таблицы: содержимое, файл на диске, интервал хука."""
from bot.repo.funnel import FunnelRepo
from bot.repo.settings import SettingsRepo
from bot.repo.users import UsersRepo
from bot.stats_export import build_workbook, export_path, make_export, stats_export_hook


class FakeDeps:
    """Мини-набор зависимостей — стате нужны только users/settings/db."""

    def __init__(self, db, admin_ids=()):
        self.db = db
        self.users = UsersRepo(db, admin_ids=admin_ids)
        self.settings = SettingsRepo(db)
        self.funnel = FunnelRepo(db, admin_ids=admin_ids)


async def test_build_workbook_excludes_admin_and_lists_users(db):
    deps = FakeDeps(db, admin_ids=(999,))
    await deps.users.upsert(1, "vasya", "Вася", source="ig")
    await deps.users.upsert(999, "admin", "Админ")

    wb = await build_workbook(deps)
    assert wb.sheetnames == ["Сводка", "Пользователи"]

    users_sheet = wb["Пользователи"]
    assert users_sheet.max_row == 2  # заголовок + один пользователь (админ исключён)
    assert users_sheet.cell(row=2, column=1).value == 1

    summary = wb["Сводка"]
    values = [cell[0].value for cell in summary.iter_rows()]
    assert "Всего" in values


async def test_make_export_writes_file_next_to_db(db):
    deps = FakeDeps(db)
    await deps.users.upsert(1, "vasya", "Вася")

    path = await make_export(deps)

    assert path == export_path(db)
    assert path.exists() and path.stat().st_size > 0


async def test_stats_export_hook_runs_once_then_waits_for_interval(db, monkeypatch):
    deps = FakeDeps(db)
    await deps.settings.set("stats_export_interval_min", "30")

    calls = {"n": 0}

    async def fake_make_export(_deps):
        calls["n"] += 1

    monkeypatch.setattr("bot.stats_export.make_export", fake_make_export)

    now = {"t": 1_000_000.0}
    monkeypatch.setattr("bot.stats_export.time.time", lambda: now["t"])

    hook = stats_export_hook(deps)

    await hook()
    assert calls["n"] == 1  # первый тик — собираем сразу, не дожидаясь интервала

    now["t"] += 60
    await hook()
    assert calls["n"] == 1  # 30 минут ещё не прошло

    now["t"] += 30 * 60
    await hook()
    assert calls["n"] == 2  # интервал наступил


async def test_stats_export_hook_survives_errors(db, monkeypatch):
    deps = FakeDeps(db)

    async def boom(_deps):
        raise RuntimeError("диск закончился")

    monkeypatch.setattr("bot.stats_export.make_export", boom)
    hook = stats_export_hook(deps)
    await hook()  # не должно бросить исключение наружу
