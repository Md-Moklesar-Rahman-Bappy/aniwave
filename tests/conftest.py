"""Shared fixtures.

Guarantees enforced here:

* No test performs a real Telegram API call - :class:`FakeBot` replaces the
  ``Bot`` object at the API boundary.
* Every test uses a temporary SQLite database, never the production file.
* No test reads or needs the real ``.env``; configuration is built explicitly.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from albums import AlbumService
from config import AppConfig, TopicConfig
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
        # Programmable failure injection.
        self.copy_error: Optional[BaseException] = None
        self.album_error: Optional[BaseException] = None
        self.send_error: Optional[BaseException] = None
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
    """A fully valid environment mapping. Contains no real credential."""
    return {
        "TELEGRAM_BOT_TOKEN": PLACEHOLDER_TOKEN,
        "SOURCE_GROUP_ID": "-1004428338491",
        "TARGET_CHANNEL": "@aniwavebd",
        "ADMIN_IDS": "6589890362",
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
SOURCE_CHAT = -1004428338491
ADMIN_ID = 6589890362


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