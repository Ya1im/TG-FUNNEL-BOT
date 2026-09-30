"""Кнопки под постом всегда одни: если текст ушёл отдельным сообщением, кнопка только под ним."""
from bot.content import ContentBlock

BTN = [{"text": "Смотреть", "url": "https://x.ru"}]


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(("text", reply_markup is not None))

    async def send_photo(self, chat_id, file_id, caption=None, reply_markup=None):
        self.sent.append(("photo", reply_markup is not None))

    async def send_video_note(self, chat_id, file_id, reply_markup=None):
        self.sent.append(("note", reply_markup is not None))

    async def send_video(self, chat_id, file_id, caption=None, reply_markup=None):
        self.sent.append(("video", reply_markup is not None))


async def run(block):
    bot = Bot()
    for factory in block.factories(bot, 1):
        await factory()
    return bot.sent


async def test_photo_with_short_caption_has_one_button_under_photo():
    assert await run(ContentBlock("Коротко", "photo", "P", BTN)) == [("photo", True)]


async def test_photo_with_long_text_has_button_only_under_text():
    sent = await run(ContentBlock("я" * 2100, "photo", "P", BTN))
    assert sent == [("photo", False), ("text", True)]


async def test_video_note_with_text_has_button_only_under_text():
    assert await run(ContentBlock("Текст", "video_note", "V", BTN)) == [("note", False), ("text", True)]


async def test_media_without_text_keeps_button_on_media():
    assert await run(ContentBlock(None, "video_note", "V", BTN)) == [("note", True)]


async def test_fallback_video_has_no_button_when_text_follows():
    calls = []

    class B:
        async def send_video(self, chat_id, file_id, caption=None, reply_markup=None):
            calls.append(reply_markup)

    with_text = ContentBlock("Текст", "video_note", "V", BTN).fallback_factory(B(), 1)
    await with_text()
    without_text = ContentBlock(None, "video_note", "V", BTN).fallback_factory(B(), 1)
    await without_text()
    assert calls[0] is None and calls[1] is not None
