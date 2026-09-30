"""Единое сообщение «фото + подпись» и кастомные эмодзи."""
from datetime import datetime
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendPhoto
from aiogram.types import Chat, Message, MessageEntity

from bot import content
from bot.content import ContentBlock, visible_len
from bot.handlers.admin.common import message_text
from bot.sender import send_block

EMOJI = '<tg-emoji emoji-id="5368324170671202286">😀</tg-emoji>'
BTN = [{"text": "Смотреть", "url": "https://x.ru"}]


@pytest.fixture(autouse=True)
def reset_caption_limit():
    content.set_caption_limit(content.PREMIUM_CAPTION_LIMIT)
    yield
    content.set_caption_limit(content.PREMIUM_CAPTION_LIMIT)


class Bot:
    """Telegram-подобный бот: подпись длиннее hard_limit отклоняет, как настоящий."""

    def __init__(self, hard_limit=1024, keep_custom_emoji=True):
        self.sent = []
        self.hard_limit = hard_limit
        self.keep_custom_emoji = keep_custom_emoji
        self._id = 0

    def _reply(self, text):
        self._id += 1
        entities = None
        if text and "<tg-emoji" in text and self.keep_custom_emoji:
            entities = [MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id="1")]
        return Message(
            message_id=self._id, date=datetime.now(), chat=Chat(id=1, type="private"),
            text=text, entities=entities,
        )

    async def send_photo(self, chat_id, file_id, caption=None, reply_markup=None):
        if caption and visible_len(caption) > self.hard_limit:
            raise TelegramBadRequest(
                method=SendPhoto(chat_id=1, photo="x"), message="Bad Request: message caption is too long"
            )
        self.sent.append(("photo", caption, reply_markup is not None))
        return self._reply(caption)

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(("text", text, reply_markup is not None))
        return self._reply(text)


def kinds(bot):
    return [s[0] for s in bot.sent]


def test_visible_len_ignores_markup_and_unescapes():
    assert visible_len(f"Привет {EMOJI} <b>мир</b> <a href=\"https://x.ru/aaaaaaaaaaaaaaa\">ссылка</a>") == len("Привет 😀 мир ссылка")
    assert visible_len("a &amp; b &lt;c&gt;") == len("a & b <c>")
    assert visible_len(None) == 0


async def test_photo_with_many_custom_emoji_stays_one_message():
    """Видимого текста 900 символов, но с разметкой эмодзи HTML > 1024 — раньше это резалось на два."""
    text = ("слово " * 100) + (EMOJI * 12)
    assert len(text) > 1024 and visible_len(text) < 1024
    bot = Bot()
    outcome = await send_block(ContentBlock(text, "photo", "P", BTN), bot, 1)
    assert outcome.ok and kinds(bot) == ["photo"] and bot.sent[0][2] is True
    assert outcome.caption_split is False


async def test_photo_with_heavy_formatting_stays_one_message():
    text = " ".join(f'<a href="https://example.com/some/long/path/{i}">ссылка {i}</a>' for i in range(30))
    assert len(text) > 1024 and visible_len(text) <= 1024
    bot = Bot()
    await send_block(ContentBlock(text, "photo", "P", BTN), bot, 1)
    assert kinds(bot) == ["photo"]


async def test_long_caption_goes_as_one_message_when_telegram_allows_it():
    bot = Bot(hard_limit=2048)
    outcome = await send_block(ContentBlock("я" * 1500, "photo", "P", BTN), bot, 1)
    assert kinds(bot) == ["photo"] and outcome.caption_split is False


async def test_long_caption_falls_back_to_split_when_telegram_refuses_and_remembers():
    bot = Bot(hard_limit=1024)
    outcome = await send_block(ContentBlock("я" * 1500, "photo", "P", BTN), bot, 1)
    assert outcome.ok and outcome.caption_split is True
    assert kinds(bot) == ["photo", "text"]
    assert bot.sent[0][1] is None                       # фото без подписи
    assert bot.sent[0][2] is False and bot.sent[1][2] is True    # кнопка одна — под текстом
    assert len(outcome.message_ids) == 2
    assert content.get_caption_limit() == 1024          # запомнили: лишних неудачных запросов не будет
    bot2 = Bot(hard_limit=1024)
    await send_block(ContentBlock("я" * 1500, "photo", "P", BTN), bot2, 1)
    assert kinds(bot2) == ["photo", "text"]


async def test_split_is_reported_for_texts_over_the_known_limit():
    content.set_caption_limit(1024)
    bot = Bot()
    outcome = await send_block(ContentBlock("я" * 1100, "photo", "P"), bot, 1)
    assert outcome.caption_split is True


async def test_short_caption_is_never_split_or_flagged():
    bot = Bot()
    outcome = await send_block(ContentBlock("Коротко", "photo", "P"), bot, 1)
    assert kinds(bot) == ["photo"] and outcome.caption_split is False


async def test_lost_custom_emoji_is_reported_when_telegram_drops_entities():
    bot = Bot(keep_custom_emoji=False)
    outcome = await send_block(ContentBlock(f"Привет {EMOJI}", None, None), bot, 1)
    assert outcome.ok and outcome.custom_emoji_lost is True


async def test_custom_emoji_kept_is_not_reported():
    bot = Bot(keep_custom_emoji=True)
    outcome = await send_block(ContentBlock(f"Привет {EMOJI}", None, None), bot, 1)
    assert outcome.custom_emoji_lost is False


async def test_plain_text_never_reports_lost_emoji():
    bot = Bot(keep_custom_emoji=False)
    outcome = await send_block(ContentBlock("Просто текст", None, None), bot, 1)
    assert outcome.custom_emoji_lost is False


async def test_unknown_result_shape_is_not_reported_as_lost():
    class Plain:
        async def send_message(self, chat_id, text, reply_markup=None):
            return SimpleNamespace(message_id=5)          # без entities — определить нельзя

    outcome = await send_block(ContentBlock(f"Привет {EMOJI}", None, None), Plain(), 1)
    assert outcome.custom_emoji_lost is False


def test_forwarded_photo_caption_keeps_custom_emoji_and_formatting():
    """Пересланный пост: сущности подписи (кастомные эмодзи, жирный, ссылка) сохраняются в тексте шага."""
    message = Message(
        message_id=1, date=datetime.now(), chat=Chat(id=1, type="private"),
        caption="Привет 😀 жирный ссылка",
        caption_entities=[
            MessageEntity(type="custom_emoji", offset=7, length=2, custom_emoji_id="5368324170671202286"),
            MessageEntity(type="bold", offset=10, length=6),
            MessageEntity(type="text_link", offset=17, length=6, url="https://x.ru"),
        ],
    )
    text = message_text(message)
    assert '<tg-emoji emoji-id="5368324170671202286">😀</tg-emoji>' in text
    assert "<b>жирный</b>" in text and '<a href="https://x.ru">ссылка</a>' in text


async def test_real_sends_update_custom_emoji_status():
    content.set_custom_emoji_status(None)
    await send_block(ContentBlock("Просто текст", None, None), Bot(), 1)
    assert content.get_custom_emoji_status() is None                   # без эмодзи ничего не узнали
    await send_block(ContentBlock(f"Привет {EMOJI}", None, None), Bot(keep_custom_emoji=False), 1)
    assert content.get_custom_emoji_status() is False
    await send_block(ContentBlock(f"Привет {EMOJI}", None, None), Bot(keep_custom_emoji=True), 1)
    assert content.get_custom_emoji_status() is True
    content.set_custom_emoji_status(None)


async def test_probe_custom_emoji_returns_true_false_or_none():
    from bot.emoji_check import probe_custom_emoji

    assert await probe_custom_emoji(Bot(keep_custom_emoji=True), 1) is True
    assert await probe_custom_emoji(Bot(keep_custom_emoji=False), 1) is False

    class Broken:
        async def send_message(self, *a, **k):
            raise TelegramBadRequest(method=SendPhoto(chat_id=1, photo="x"), message="Bad Request: chat not found")

    assert await probe_custom_emoji(Broken(), 1) is None                # проверить не удалось — не гадаем
