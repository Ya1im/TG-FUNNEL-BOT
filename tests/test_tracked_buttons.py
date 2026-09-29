from bot.content import ContentBlock, button_hash
from bot.handlers.admin.common import buttons_hint, parse_buttons

URL = "https://tkpdt.ru/lendingi150"


def test_parse_buttons_track_flag():
    assert parse_buttons(f"Смотреть урок | {URL} | клик") == [
        {"text": "Смотреть урок", "url": URL, "track": True}
    ]
    assert parse_buttons(f"Смотреть урок | {URL} | КЛИК") == [
        {"text": "Смотреть урок", "url": URL, "track": True}
    ]


def test_parse_buttons_without_flag_is_plain_link():
    assert parse_buttons(f"Смотреть урок | {URL}") == [{"text": "Смотреть урок", "url": URL}]
    assert parse_buttons(f"Смотреть урок | {URL} | что-то") == [{"text": "Смотреть урок", "url": URL}]


def test_buttons_hint_marks_tracked():
    import json

    raw = json.dumps([{"text": "A", "url": URL, "track": True}, {"text": "B", "url": URL}])
    assert buttons_hint(raw) == "A 🎯, B"


def test_keyboard_tracked_button_is_callback():
    block = ContentBlock(text="x", buttons=[{"text": "Смотреть", "url": URL, "track": True}], track=True)
    btn = block.keyboard().inline_keyboard[0][0]
    assert btn.url is None
    assert btn.callback_data == "lc:" + button_hash(URL)
    assert len(btn.callback_data.encode()) <= 64


def test_keyboard_default_keeps_url_even_for_tracked_button():
    block = ContentBlock(text="x", buttons=[{"text": "Смотреть", "url": URL, "track": True}])
    btn = block.keyboard().inline_keyboard[0][0]
    assert btn.url == URL
    assert btn.callback_data is None


def test_keyboard_plain_button_stays_url_when_tracking_enabled():
    block = ContentBlock(text="x", buttons=[{"text": "Разбор", "url": URL}], track=True)
    btn = block.keyboard().inline_keyboard[0][0]
    assert btn.url == URL


def test_button_hash_is_stable_and_short():
    assert button_hash(URL) == button_hash(URL)
    assert len(button_hash(URL)) == 16
    assert button_hash(URL) != button_hash(URL + "x")


def test_parse_buttons_flag_word_never_leaks_into_url():
    assert parse_buttons(f"Урок | {URL} | клик") == [{"text": "Урок", "url": URL, "track": True}]
    assert parse_buttons(f"Урок | {URL}|клик") == [{"text": "Урок", "url": URL, "track": True}]
    assert parse_buttons(f"Урок | {URL} | ") == [{"text": "Урок", "url": URL}]
