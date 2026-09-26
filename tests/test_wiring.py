"""Проверяем, что всё собирается: роутеры, хендлеры, планировщик, бэкап."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.backup import make_backup
from bot.config import Config
from bot.deps import Deps
from bot.handlers.admin.broadcast import parse_schedule
from bot.handlers.admin.common import parse_buttons, preview, safe_excerpt
from tests.conftest import fresh_dispatcher

TZ = ZoneInfo("Europe/Moscow")
FAKE_TOKEN = "123456789:AAFakeTokenForTestsOnly_0123456789ab"


@pytest.fixture
def config():
    return Config(
        bot_token=FAKE_TOKEN, admin_ids=(99,), db_path=":memory:",
        messages_per_second=20, tick_seconds=60,
    )


async def test_dispatcher_builds_with_all_routers(db, config):
    bot = Bot(FAKE_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    deps = Deps.build(config, db, bot)
    dp = fresh_dispatcher(deps)
    names = {r.name for r in dp.sub_routers[0].sub_routers[0].sub_routers}
    assert {"admin-media", "admin-funnel", "admin-material", "admin-settings",
            "admin-stats", "admin-broadcast"} <= names
    await bot.session.close()


def test_config_parses_admins():
    config = Config.from_env(
        {"BOT_TOKEN": FAKE_TOKEN, "ADMIN_IDS": "1, 2;3", "MESSAGES_PER_SECOND": "25"}
    )
    assert config.admin_ids == (1, 2, 3)
    assert config.messages_per_second == 25
    assert config.is_admin(2) and not config.is_admin(9)


def test_config_requires_token():
    with pytest.raises(RuntimeError):
        Config.from_env({"ADMIN_IDS": "1"})


def test_parse_schedule_time_today_and_tomorrow():
    now = datetime(2026, 9, 15, 12, 0, tzinfo=TZ)
    assert parse_schedule("21:30", now) == int(datetime(2026, 9, 15, 21, 30, tzinfo=TZ).timestamp())
    # время уже прошло — переносим на завтра
    assert parse_schedule("09:00", now) == int(datetime(2026, 9, 16, 9, 0, tzinfo=TZ).timestamp())
    assert parse_schedule("завтра 10:00", now) == int(
        datetime(2026, 9, 16, 10, 0, tzinfo=TZ).timestamp()
    )


def test_parse_schedule_with_date():
    now = datetime(2026, 9, 15, 12, 0, tzinfo=TZ)
    assert parse_schedule("20.09 14:00", now) == int(
        datetime(2026, 9, 20, 14, 0, tzinfo=TZ).timestamp()
    )
    assert parse_schedule("как-нибудь потом", now) is None
    assert parse_schedule("99:99", now) is None


def test_parse_buttons():
    raw = "Канал | https://t.me/x\nмусор\nСайт|https://example.com"
    assert parse_buttons(raw) == [
        {"text": "Канал", "url": "https://t.me/x"},
        {"text": "Сайт", "url": "https://example.com"},
    ]


def _assert_valid_telegram_html(text: str) -> None:
    """Грубая имитация проверки HTML, которую делает Telegram: теги должны
    быть закрыты и правильно вложены, иначе бот получит `can't parse
    entities` и не сможет показать сообщение."""
    from html.parser import HTMLParser

    class _Strict(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.stack: list[str] = []

        def handle_starttag(self, tag, attrs):
            self.stack.append(tag)

        def handle_endtag(self, tag):
            assert self.stack and self.stack[-1] == tag, f"незакрытый/неверно вложенный <{tag}>"
            self.stack.pop()

    parser = _Strict()
    parser.feed(text)
    parser.close()
    assert not parser.stack, f"незакрытые теги: {parser.stack}"


def test_preview_shortens():
    assert preview(None) == "без текста"
    assert preview("а" * 100).endswith("…")


def test_preview_never_breaks_html_on_long_rich_text():
    """Регресс: раньше preview() резала HTML "как есть" и на длинном
    отформатированном тексте обрезка попадала внутрь тега/сущности —
    получившийся битый HTML Telegram не принимал, и кнопка «Тексты и
    кнопки» / список материалов зависала (шла загрузка и ничего не
    происходило), потому что show() падал раньше, чем хендлер успевал
    ответить на callback."""
    rich_text = (
        "<b>Заголовок</b> с очень длинным описанием и ссылкой "
        '<a href="https://example.com/some/very/long/path?x=1&amp;y=2">переходи сюда</a> '
        "а также амперсанд Tom &amp; Jerry, и ещё текста " + "слово " * 40
    )
    for limit in range(1, 80):
        _assert_valid_telegram_html(preview(rich_text, limit))


def test_safe_excerpt_short_text_passthrough_and_long_text_stays_valid():
    assert safe_excerpt(None) == ""
    short = "<b>жирный</b> короткий текст"
    assert safe_excerpt(short) == short  # короткий текст не трогаем — форматирование сохраняется

    long_rich = "<b>Материал</b>: " + "<i>слово</i> " * 500 + '<a href="https://x.test">ссылка</a>'
    excerpt = safe_excerpt(long_rich, limit=200)
    _assert_valid_telegram_html(excerpt)
    assert len(excerpt) < len(long_rich)


async def test_backup_creates_file(db):
    path = await make_backup(db, keep=3)
    assert path.exists() and path.stat().st_size > 0


def test_session_is_default_without_proxy_settings():
    from bot.__main__ import build_session

    config = Config.from_env({"BOT_TOKEN": FAKE_TOKEN, "ADMIN_IDS": "1"})
    assert build_session(config) is None


def test_session_uses_api_mirror():
    from bot.__main__ import build_session

    config = Config.from_env(
        {"BOT_TOKEN": FAKE_TOKEN, "ADMIN_IDS": "1", "TELEGRAM_API_BASE": "https://tg.example.dev/"}
    )
    session = build_session(config)
    assert "tg.example.dev" in session.api.base
    assert "tg.example.dev" in session.api.api_url(token="X", method="getMe")


def test_session_uses_socks_proxy():
    from bot.__main__ import build_session

    config = Config.from_env(
        {"BOT_TOKEN": FAKE_TOKEN, "ADMIN_IDS": "1", "TELEGRAM_PROXY": "socks5://172.17.0.1:1080"}
    )
    session = build_session(config)
    assert session.proxy == "socks5://172.17.0.1:1080"
