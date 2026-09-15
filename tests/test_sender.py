
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage

from bot.content import ContentBlock
from bot.repo.users import UsersRepo
from bot.sender import BLOCKED, FAILED, SENT, RateLimiter, safe_send, send_block

METHOD = SendMessage(chat_id=1, text="x")


class FakeClock:
    """Часы, которые двигаются только когда мы «спим»."""

    def __init__(self):
        self.now = 0.0
        self.slept = 0.0

    def time(self):
        return self.now

    async def sleep(self, seconds):
        self.slept += seconds
        self.now += seconds


async def test_rate_limiter_respects_speed():
    clock = FakeClock()
    limiter = RateLimiter(20, clock=clock.time, sleep=clock.sleep)
    for _ in range(40):
        await limiter.acquire()
    # 40 отправок при 20/сек — почти две секунды
    assert 1.9 <= clock.slept <= 2.1


async def test_no_limit_when_rate_is_zero():
    clock = FakeClock()
    limiter = RateLimiter(0, clock=clock.time, sleep=clock.sleep)
    for _ in range(10):
        await limiter.acquire()
    assert clock.slept == 0


async def test_safe_send_ok():
    async def action():
        return "message"

    outcome = await safe_send(action)
    assert outcome.status == SENT and outcome.result == "message"


async def test_safe_send_marks_blocked(db):
    users = UsersRepo(db)
    await users.upsert(42)

    async def action():
        raise TelegramForbiddenError(method=METHOD, message="bot was blocked by the user")

    outcome = await safe_send(action, chat_id=42, users=users)
    assert outcome.status == BLOCKED
    assert (await users.get(42))["status"] == "blocked"


async def test_safe_send_retries_after_429():
    calls = {"n": 0}
    slept = []

    async def action():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TelegramRetryAfter(method=METHOD, message="flood", retry_after=3)
        return "ok"

    async def sleep(sec):
        slept.append(sec)

    outcome = await safe_send(action, sleep=sleep)
    assert outcome.status == SENT and calls["n"] == 2
    assert slept == [3.5]


async def test_safe_send_gives_up_on_bad_request():
    async def action():
        raise TelegramBadRequest(method=METHOD, message="chat not found")

    outcome = await safe_send(action)
    assert outcome.status == FAILED and "chat not found" in outcome.error


async def test_safe_send_exhausts_retries():
    async def action():
        raise TelegramRetryAfter(method=METHOD, message="flood", retry_after=1)

    async def sleep(_):
        return None

    outcome = await safe_send(action, retries=2, sleep=sleep)
    assert outcome.status == FAILED


class FakeBot:
    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.calls.append(("text", chat_id, text, reply_markup))
        return "msg"

    async def send_video_note(self, chat_id, file_id, reply_markup=None):
        self.calls.append(("video_note", chat_id, file_id, reply_markup))
        return "note"

    async def send_photo(self, chat_id, file_id, caption=None, reply_markup=None):
        self.calls.append(("photo", chat_id, file_id, caption, reply_markup))
        return "photo"


async def test_video_note_with_text_sends_two_messages():
    bot = FakeBot()
    block = ContentBlock(text="Привет", media_kind="video_note", file_id="F")
    await send_block(block, bot, 1)
    assert [c[0] for c in bot.calls] == ["video_note", "text"]


async def test_photo_uses_caption():
    bot = FakeBot()
    block = ContentBlock(text="Подпись", media_kind="photo", file_id="F")
    await send_block(block, bot, 1)
    assert bot.calls == [("photo", 1, "F", "Подпись", None)]


async def test_long_caption_goes_as_separate_message():
    bot = FakeBot()
    block = ContentBlock(text="я" * 1500, media_kind="photo", file_id="F")
    await send_block(block, bot, 1)
    assert [c[0] for c in bot.calls] == ["photo", "text"]
    assert bot.calls[0][3] is None


async def test_buttons_render_and_name_substitution():
    bot = FakeBot()
    block = ContentBlock(
        text="Привет, {name}!", buttons=[{"text": "Канал", "url": "https://t.me/x"}]
    )

    class FakeUser:
        first_name = "Вася"
        username = "vasya"

    await send_block(block, bot, 1, user=FakeUser())
    kind, chat_id, text, kb = bot.calls[0]
    assert text == "Привет, Вася!"
    assert kb.inline_keyboard[0][0].text == "Канал"
