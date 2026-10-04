"""Shared fixtures.

Guarantees enforced here:

* No test performs a real Telegram API call - :class:`FakeBot` replaces the
  ``Bot`` object at the API boundary.
* Every test uses a temporary SQLite database, never the production file.
* No test reads or needs the real ``.env``; configuration is built explicitly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from albums import AlbumService
from config import AppConfig
from database import Database
from handlers import Handlers
from publisher import Publisher

#: Fixed clock base so time-dependent behaviour is deterministic.
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class Clock:
    """Manually advanceable clock for album quiet-period tests."""

    def __init__(self, start: datetime = T0) -> None:
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current = self.current + timedelta(seconds=seconds)


class FakeMessage:
    """Minimal stand-in for telegram.Message returned by bot calls."""

    def __init__(self, message_id: int, text: Optional[str] = None) -> None:
        self.message_id = message_id
        self.text = text
        self.chat_id: Optional[int] = None


class FakeBot:
    """Records outbound calls instead of contacting Telegram."""

    def __init__(self) -> None:
        self._next_id = 9000
        self.copy_calls: List[Dict[str, Any]] = []
        self.album_calls: List[Dict[str, Any]] = []
        self.sent_messages: List[Dict[str, Any]] = []
        self.edit_calls: List[Dict[str, Any]] = []
        # Per-method call records, so tests can assert *which* send path was used.
        self.send_video_calls: List[Dict[str, Any]] = []
        self.send_document_calls: List[Dict[str, Any]] = []
        self.send_photo_calls: List[Dict[str, Any]] = []
        self.send_audio_calls: List[Dict[str, Any]] = []
        self.send_animation_calls: List[Dict[str, Any]] = []
        # Programmable failure injection.
        self.copy_error: Optional[BaseException] = None
        self.album_error: Optional[BaseException] = None
        self.send_error: Optional[BaseException] = None
        self.video_error: Optional[BaseException] = None
        self.document_error: Optional[BaseException] = None
        self.username = "aniwave_test_bot"

    def _allocate(self) -> int:
        self._next_id += 1
        return self._next_id

    async def get_me(self):
        class _Me:
            username = "aniwave_test_bot"
            id = 1
            first_name = "aniwave"

        return _Me()

    # -- concrete send_* boundaries used by Publisher --------------------- #

    async def send_video(self, *, chat_id, video, **kwargs):
        self.send_video_calls.append({"chat_id": chat_id, "video": video, **kwargs})
        if self.video_error is not None:
            raise self.video_error
        return FakeMessage(self._allocate())

    async def send_document(self, *, chat_id, document, **kwargs):
        self.send_document_calls.append({"chat_id": chat_id, "document": document, **kwargs})
        if self.document_error is not None:
            raise self.document_error
        return FakeMessage(self._allocate())

    async def send_photo(self, *, chat_id, photo, **kwargs):
        self.send_photo_calls.append({"chat_id": chat_id, "photo": photo, **kwargs})
        if self.send_error is not None:
            raise self.send_error
        return FakeMessage(self._allocate())

    async def send_audio(self, *, chat_id, audio, **kwargs):
        self.send_audio_calls.append({"chat_id": chat_id, "audio": audio, **kwargs})
        if self.send_error is not None:
            raise self.send_error
        return FakeMessage(self._allocate())

    async def send_animation(self, *, chat_id, animation, **kwargs):
        self.send_animation_calls.append({"chat_id": chat_id, "animation": animation, **kwargs})
        if self.send_error is not None:
            raise self.send_error
        return FakeMessage(self._allocate())

    # -- legacy copy_message (albums / older paths) ---------------------- #

    async def copy_message(self, *, chat_id, from_chat_id, message_id, **kwargs):
        self.copy_calls.append(
            {
                "chat_id": chat_id,
                "from_chat_id": from_chat_id,
                "message_id": message_id,
                "caption": kwargs.get("caption"),
                "parse_mode": kwargs.get("parse_mode"),
                "extra_keys": sorted(kwargs),
            }
        )
        if self.copy_error is not None:
            raise self.copy_error
        return FakeMessage(self._allocate())

    async def send_media_group(self, *, chat_id, media):
        self.album_calls.append({"chat_id": chat_id, "media": list(media)})
        if self.album_error is not None:
            raise self.album_error
        return [FakeMessage(self._allocate()) for _ in media]

    async def send_message(self, *, chat_id, text, message_thread_id=None, reply_markup=None, **kwargs):
        self.sent_messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "message_thread_id": message_thread_id,
                "reply_markup": reply_markup,
            }
        )
        if self.send_error is not None:
            raise self.send_error
        return FakeMessage(self._allocate(), text)

    async def edit_message_text(self, *, chat_id=None, message_id=None, text, reply_markup=None, **kwargs):
        self.edit_calls.append(
            {"chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": reply_markup}
        )
        return FakeMessage(message_id or self._allocate(), text)


# --------------------------------------------------------------------------- #
# Environment / configuration
# --------------------------------------------------------------------------- #


def fake_token(marker: str = "z") -> str:
    """Build a token-*shaped* test string at runtime.

    Assembled from parts on purpose: no source file may contain a literal that a
    secret scanner would flag, while tests still exercise real redaction.
    """
    return "123456789:AA" + (marker * 40)[:35]


#: A syntactically valid but deliberately non-token-shaped placeholder.
PLACEHOLDER_TOKEN = "123456789:AAplaceholder"


@pytest.fixture
def env() -> Dict[str, str]:
    """A fully valid environment mapping. Contains no real credential.

    Mirrors the repository's real topic layout: ``WEB_SERIES_TOPIC_ID`` is the
    declared topic in ``config.TOPIC_SPECS``; the three anime variables are
    registered through dynamic ``<NAME>_TOPIC_ID`` discovery.
    """
    return {
        "TELEGRAM_BOT_TOKEN": PLACEHOLDER_TOKEN,
        "SOURCE_GROUP_ID": "-1004428338491",
        "TARGET_CHANNEL": "@aniwavebd",
        "ADMIN_IDS": "6589890362",
        "WEB_SERIES_TOPIC_ID": "33",
        "ONE_PIECE_TOPIC_ID": "23",
        "NARUTO_TOPIC_ID": "6",
        "BLEACH_TOPIC_ID": "8",
        "AUTO_PUBLISH": "false",
        "INCLUDE_HD_CLAIM": "false",
        "TIMEZONE": "Asia/Dhaka",
        "DATABASE_PATH": "unused.db",
        "LOG_LEVEL": "INFO",
    }


@pytest.fixture
def config(env, tmp_path) -> AppConfig:
    cfg = AppConfig.load(env)
    return cfg


@pytest.fixture
def auto_config(env, tmp_path) -> AppConfig:
    """Configuration with automatic publishing enabled."""
    values = dict(env)
    values["AUTO_PUBLISH"] = "true"
    return AppConfig.load(values)


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #


@pytest.fixture
async def db(tmp_path) -> Database:
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def db_path(tmp_path) -> str:
    return str(tmp_path / "direct.db")


# --------------------------------------------------------------------------- #
# Telegram boundary + services
# --------------------------------------------------------------------------- #


@pytest.fixture
def fake_bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def publisher(fake_bot, db, config) -> Publisher:
    return Publisher(fake_bot, db, config)  # type: ignore[arg-type]


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def album_service(db, publisher, fake_bot, config, clock) -> AlbumService:
    return AlbumService(db, publisher, fake_bot, config, now_fn=clock)  # type: ignore[arg-type]


@pytest.fixture
def handlers(db, publisher, album_service, config) -> Handlers:
    return Handlers(db, publisher, album_service, config)


# --------------------------------------------------------------------------- #
# Sample data helpers
# --------------------------------------------------------------------------- #

ONE_PIECE_TOPIC = 23
NARUTO_TOPIC = 6
BLEACH_TOPIC = 8
WEB_SERIES_TOPIC = 33
SOURCE_CHAT = -1004428338491
ADMIN_ID = 6589890362


# --------------------------------------------------------------------------- #
# Real python-telegram-bot update builders
# --------------------------------------------------------------------------- #
# These construct genuine telegram.* objects rather than stubs. That fidelity
# matters: PTB models an absent ``photo`` as an empty tuple ``()`` while absent
# ``video``/``document`` are ``None``. Hand-rolled stubs that used ``None``
# everywhere hid the resulting misclassification bug, so media tests must use
# real objects.

from telegram import Animation, Audio, Chat, Document, Message, PhotoSize, Update, User, Video


def make_chat(chat_id: int = SOURCE_CHAT, chat_type: str = "supergroup") -> Chat:
    return Chat(id=chat_id, type=chat_type, title="AniWave Database")


def make_user(user_id: int = ADMIN_ID, is_bot: bool = False) -> User:
    return User(id=user_id, first_name="admin", is_bot=is_bot)


MKV_FILENAME = (
    "Suits S01E01 1080p BluRay x265 HEVC ESub "
    "[Dual Audio][Hindi 2.0+English 5.1]-KmHD.mkv"
)
MKV_CAPTION = (
    "Suits S01E01 1080p BluRay x265 HEVC ESub\n"
    "[Dual Audio][Hindi 2.0+English 5.1]-KmHD.mkv"
)


def build_document_update(
    *,
    file_name: Optional[str] = MKV_FILENAME,
    mime_type: Optional[str] = "video/x-matroska",
    caption: Optional[str] = MKV_CAPTION,
    file_size: int = 1_288_490_188,
    message_id: int = 555,
    thread_id: Optional[int] = WEB_SERIES_TOPIC,
    user_id: int = ADMIN_ID,
    is_bot: bool = False,
    chat_id: int = SOURCE_CHAT,
    media_group_id: Optional[str] = None,
    update_id: int = 1001,
) -> Update:
    """A genuine ``Update`` carrying a document (the failing MKV case)."""
    document = Document(
        file_id="BQACAgQDOCFILEIDMKV",
        file_unique_id="AgADDOCUNIQUEIDMKV",
        file_name=file_name,
        mime_type=mime_type,
        file_size=file_size,
    )
    message = Message(
        message_id=message_id,
        date=None,
        chat=make_chat(chat_id),
        from_user=make_user(user_id, is_bot=is_bot),
        message_thread_id=thread_id,
        caption=caption,
        document=document,
        media_group_id=media_group_id,
    )
    return Update(update_id=update_id, message=message)


def attach_fake_bot(update: Update, bot) -> Update:
    """Let PTB ``Message`` shortcuts work against the fake bot.

    Real ``telegram.Message`` objects refuse to ``reply_text`` without an
    associated bot. Attaching :class:`FakeBot` keeps the objects genuine while
    routing every shortcut through the recorded fake - no network involved.
    """
    message = update.effective_message
    if message is not None:
        message.set_bot(bot)
    return update


def build_video_update(**overrides) -> Update:
    """A genuine ``Update`` carrying a native Telegram video."""
    kwargs = {
        "caption": "Suits EP 1",
        "message_id": 556,
        "thread_id": WEB_SERIES_TOPIC,
        "update_id": 1002,
    }
    kwargs.update(overrides)
    video = Video(
        file_id="BAACAgQVIDEOFILEIDMP4",
        file_unique_id="AgADVIDEOUNIQUEID",
        width=1920, height=1080, duration=2700,
        file_name="Suits S01E01.mp4", mime_type="video/mp4",
        file_size=500_000_000,
    )
    message = Message(
        message_id=kwargs["message_id"],
        date=None,
        chat=make_chat(kwargs.get("chat_id", SOURCE_CHAT)),
        from_user=make_user(kwargs.get("user_id", ADMIN_ID)),
        message_thread_id=kwargs["thread_id"],
        caption=kwargs["caption"],
        video=video,
    )
    return Update(update_id=kwargs["update_id"], message=message)


def build_photo_update(**overrides) -> Update:
    kwargs = {"caption": "Suits EP 1", "message_id": 560, "thread_id": WEB_SERIES_TOPIC,
              "update_id": 1003}
    kwargs.update(overrides)
    photo = PhotoSize(file_id="SMALLFILEID", file_unique_id="SMALLUNIQUE", width=90, height=90)
    large = PhotoSize(file_id="LARGEPHOTOFILEID", file_unique_id="LARGEUNIQUE", width=1280, height=720)
    message = Message(
        message_id=kwargs["message_id"],
        date=None,
        chat=make_chat(kwargs.get("chat_id", SOURCE_CHAT)),
        from_user=make_user(kwargs.get("user_id", ADMIN_ID)),
        message_thread_id=kwargs["thread_id"],
        caption=kwargs["caption"],
        photo=(photo, large),
    )
    return Update(update_id=kwargs["update_id"], message=message)


def build_audio_update(**overrides) -> Update:
    kwargs = {"caption": "Suits EP 1", "message_id": 561, "thread_id": WEB_SERIES_TOPIC,
              "update_id": 1004}
    kwargs.update(overrides)
    audio = Audio(file_id="CQACAgQAUDIOFILEID", file_unique_id="AgADAUDIOUNIQUE",
                  duration=1800, performer="Artist", title="Track")
    message = Message(
        message_id=kwargs["message_id"],
        date=None,
        chat=make_chat(kwargs.get("chat_id", SOURCE_CHAT)),
        from_user=make_user(kwargs.get("user_id", ADMIN_ID)),
        message_thread_id=kwargs["thread_id"],
        caption=kwargs["caption"],
        audio=audio,
    )
    return Update(update_id=kwargs["update_id"], message=message)


def build_animation_update(**overrides) -> Update:
    kwargs = {"caption": "Suits EP 1", "message_id": 562, "thread_id": WEB_SERIES_TOPIC,
              "update_id": 1005}
    kwargs.update(overrides)
    animation = Animation(file_id="BAACAgQANIMFILEID", file_unique_id="AgADANIMUNIQUE",
                          width=480, height=480, duration=12)
    message = Message(
        message_id=kwargs["message_id"],
        date=None,
        chat=make_chat(kwargs.get("chat_id", SOURCE_CHAT)),
        from_user=make_user(kwargs.get("user_id", ADMIN_ID)),
        message_thread_id=kwargs["thread_id"],
        caption=kwargs["caption"],
        animation=animation,
    )
    return Update(update_id=kwargs["update_id"], message=message)


def build_text_update(text: str = "/topicid", **overrides) -> Update:
    from telegram import MessageEntity

    kwargs = {"message_id": 570, "thread_id": WEB_SERIES_TOPIC, "update_id": 1006}
    kwargs.update(overrides)
    message = Message(
        message_id=kwargs["message_id"],
        date=None,
        chat=make_chat(kwargs.get("chat_id", SOURCE_CHAT)),
        from_user=make_user(kwargs.get("user_id", ADMIN_ID)),
        message_thread_id=kwargs["thread_id"],
        text=text,
        entities=[MessageEntity(type="bot_command", offset=0, length=len(text))],
    )
    return Update(update_id=kwargs["update_id"], message=message)


@pytest.fixture
def item_payload() -> Dict[str, Any]:
    return {
        "source_chat_id": SOURCE_CHAT,
        "source_message_id": 100,
        "topic_id": ONE_PIECE_TOPIC,
        "sender_id": ADMIN_ID,
        "media_type": "video",
        "file_id": "file-abc",
        "file_unique_id": "uniq-abc",
        "caption": "One Piece EP 1165",
        "anime_title": "One Piece",
        "emoji": "\U0001F4FA",
        "episode_number": "1165",
    }