"""Regression tests for the MKV-as-document upload defect.

Root cause
----------
``handlers.describe_media_type`` probed media attributes with
``getattr(message, attr, None) is not None``. In PTB 21.11.1 ``Message.photo`` is
an **empty tuple** ``()`` when there is no photo, so the probe matched ``photo``
for *every* message - including documents - and returned ``"photo"``.
``extract_file_id(message, "photo")`` then found no sizes, returned ``None`` and
``handle_media`` bailed out **before** inserting a database row. Document
uploads such as ``.mkv`` were therefore dropped silently.

These tests use **real** ``telegram.*`` objects so PTB's empty-tuple semantics
are exercised, and they assert the whole pipeline: filter match -> routing ->
database insert -> publishing dispatch. No Telegram API call is ever made.
"""

from __future__ import annotations

import pytest
from telegram.error import BadRequest, TimedOut

from database import SCHEMA_VERSION, Status
from handlers import SUPPORTED_MEDIA
from media import (
    NATIVE_VIDEO_DOCUMENT_EXTENSIONS,
    VIDEO_DOCUMENT_EXTENSIONS,
    detect_telegram_media_type,
    extension_of,
    extract_media,
    is_video_document,
)
from tests.conftest import (
    attach_fake_bot,
    MKV_CAPTION,
    MKV_FILENAME,
    SOURCE_CHAT,
    WEB_SERIES_TOPIC,
    build_animation_update,
    build_audio_update,
    build_document_update,
    build_photo_update,
    build_text_update,
    build_video_update,
)


# --------------------------------------------------------------------------- #
# The defect itself
# --------------------------------------------------------------------------- #


def test_absent_photo_is_an_empty_tuple_not_none():
    """The exact PTB behaviour that broke the old probe."""
    message = build_document_update().message
    assert message.photo == ()
    assert message.photo is not None  # the old `is not None` probe matched this
    assert not message.photo


def test_document_is_not_misclassified_as_photo():
    assert detect_telegram_media_type(build_document_update().message) == "document"


def test_every_supported_type_is_detected():
    assert detect_telegram_media_type(build_document_update().message) == "document"
    assert detect_telegram_media_type(build_video_update().message) == "video"
    assert detect_telegram_media_type(build_photo_update().message) == "photo"
    assert detect_telegram_media_type(build_audio_update().message) == "audio"
    assert detect_telegram_media_type(build_animation_update().message) == "animation"


def test_text_message_has_no_media_type():
    assert detect_telegram_media_type(build_text_update().message) is None


# --------------------------------------------------------------------------- #
# Filter acceptance / rejection
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("builder", [
    build_document_update, build_video_update, build_photo_update,
    build_audio_update, build_animation_update,
])
def test_filter_accepts_every_supported_type(builder):
    assert SUPPORTED_MEDIA.filter(builder().message) is True


def test_filter_accepts_mkv_document():
    assert SUPPORTED_MEDIA.filter(build_document_update().message) is True


def test_filter_rejects_plain_text():
    assert SUPPORTED_MEDIA.filter(build_text_update(text="hello").message) is False


def test_filter_rejects_command_text():
    assert SUPPORTED_MEDIA.filter(build_text_update(text="/status").message) is False


def test_filter_rejects_sticker():
    from telegram import Sticker

    from tests.conftest import make_chat, make_user

    message = type(build_text_update().message)(
        message_id=1, date=None, chat=make_chat(), from_user=make_user(),
        message_thread_id=WEB_SERIES_TOPIC,
        sticker=Sticker(file_id="S", file_unique_id="SU", width=1, height=1,
                        is_animated=False, is_video=False, type="regular"),
    )
    assert SUPPORTED_MEDIA.filter(message) is False


def test_filter_is_not_filters_all():
    from telegram.ext import filters

    assert SUPPORTED_MEDIA is not filters.ALL


def test_media_handler_does_not_consume_commands():
    """A command must not be swallowed by the media handler."""
    assert SUPPORTED_MEDIA.check_update(build_text_update(text="/topicid")) is False


# --------------------------------------------------------------------------- #
# Extraction and classification
# --------------------------------------------------------------------------- #


def test_mkv_extraction():
    info = extract_media(build_document_update().message)
    assert info is not None
    assert info.telegram_type == "document"
    assert info.logical_type == "video_document"
    assert info.extension == ".mkv"
    assert info.file_id == "BQACAgQDOCFILEIDMKV"
    assert info.file_unique_id == "AgADDOCUNIQUEIDMKV"
    assert info.mime_type == "video/x-matroska"
    assert info.file_size == 1_288_490_188
    assert info.source_chat_id == SOURCE_CHAT
    assert info.source_message_id == 555
    assert info.topic_id == WEB_SERIES_TOPIC


def test_caption_is_carried_through():
    info = extract_media(build_document_update().message)
    assert info.caption == MKV_CAPTION


@pytest.mark.parametrize("name", ["a.mkv", "A.MKV", "Show.S01.MkV", "x.MkV"])
def test_extension_matching_is_case_insensitive(name):
    assert extension_of(name) == ".mkv"
    assert is_video_document(file_name=name) is True


def test_octet_stream_with_mkv_extension_is_video_document():
    """Telegram often reports application/octet-stream; the extension wins."""
    info = extract_media(build_document_update(mime_type="application/octet-stream").message)
    assert info.logical_type == "video_document"


def test_mkv_is_not_preferred_as_native_video():
    """Telegram cannot transcode Matroska, so MKV must go out as a document."""
    info = extract_media(build_document_update().message)
    assert info.prefers_native_video is False
    assert ".mkv" not in NATIVE_VIDEO_DOCUMENT_EXTENSIONS
    assert ".mkv" in VIDEO_DOCUMENT_EXTENSIONS


def test_mp4_document_extraction_prefers_native_video():
    info = extract_media(
        build_document_update(file_name="Episode 1.mp4", mime_type="video/mp4").message
    )
    assert info.logical_type == "video_document"
    assert info.prefers_native_video is True


def test_non_video_document_is_plain_document():
    info = extract_media(
        build_document_update(file_name="notes.pdf", mime_type="application/pdf").message
    )
    assert info.telegram_type == "document"
    assert info.logical_type == "document"
    assert info.prefers_native_video is False


def test_missing_filename_falls_back_to_mime():
    info = extract_media(
        build_document_update(file_name=None, mime_type="video/x-matroska").message
    )
    assert info.file_name is None
    assert info.extension == ""
    assert info.logical_type == "video_document"


def test_video_mime_without_extension_is_video_document():
    info = extract_media(
        build_document_update(file_name=None, mime_type="video/webm").message
    )
    assert info.logical_type == "video_document"


def test_photo_uses_largest_size():
    info = extract_media(build_photo_update().message)
    assert info.file_id == "LARGEPHOTOFILEID"


def test_extract_media_returns_none_for_text():
    assert extract_media(build_text_update().message) is None


# --------------------------------------------------------------------------- #
# End-to-end: automatic mode, database insert, publishing dispatch
# --------------------------------------------------------------------------- #


async def test_mkv_reaches_database_and_publisher(db, publisher, album_service,
                                                   auto_config, fake_bot):
    """The full production path for the reported defect."""
    from handlers import Handlers

    handlers = Handlers(db, publisher, album_service, auto_config)
    handlers.bot = fake_bot

    update = attach_fake_bot(build_document_update(), fake_bot)

    # 1. topic 33 resolves to Web Series
    topic = auto_config.topic_for(WEB_SERIES_TOPIC)
    assert topic is not None and topic.title == "Web Series"

    # 2. filter accepts it
    assert SUPPORTED_MEDIA.check_update(update) is True

    # 3. routing accepts it
    route = handlers._media_route(update)
    assert route is not None
    assert route[0] == WEB_SERIES_TOPIC

    # 4. the handler inserts a row and dispatches publishing
    await handlers.handle_media(update, _context())

    row = await db.get_item_by_source(SOURCE_CHAT, 555)
    assert row is not None, "MKV upload must reach database insertion"
    assert row["status"] == Status.PUBLISHED
    assert row["telegram_media_type"] == "document"
    assert row["logical_media_type"] == "video_document"
    assert row["file_name"] == MKV_FILENAME
    assert row["mime_type"] == "video/x-matroska"
    assert row["file_size"] == 1_288_490_188
    assert row["anime_title"] == "Web Series"
    assert row["target_message_id"]

    # 5. an MKV is published as a document, never as a native video
    assert len(fake_bot.send_document_calls) == 1
    assert fake_bot.send_document_calls[0]["document"] == "BQACAgQDOCFILEIDMKV"
    assert fake_bot.send_video_calls == []


async def test_native_video_uses_send_video_with_streaming(db, publisher, album_service,
                                                           auto_config, fake_bot):
    from handlers import Handlers

    handlers = Handlers(db, publisher, album_service, auto_config)
    handlers.bot = fake_bot

    await handlers.handle_media(attach_fake_bot(build_video_update(), fake_bot), _context())

    row = await db.get_item_by_source(SOURCE_CHAT, 556)
    assert row is not None
    assert row["status"] == Status.PUBLISHED
    assert row["telegram_media_type"] == "video"

    assert len(fake_bot.send_video_calls) == 1
    call = fake_bot.send_video_calls[0]
    assert call["video"] == "BAACAgQVIDEOFILEIDMP4"
    assert call["supports_streaming"] is True
    assert fake_bot.send_document_calls == []


async def test_mp4_document_prefers_native_video(db, publisher, album_service,
                                                  auto_config, fake_bot):
    from handlers import Handlers

    handlers = Handlers(db, publisher, album_service, auto_config)
    handlers.bot = fake_bot

    update = attach_fake_bot(
        build_document_update(file_name="Suits S01E01.mp4", mime_type="video/mp4",
                           message_id=580),
        fake_bot,
    )
    await handlers.handle_media(update, _context())

    row = await db.get_item_by_source(SOURCE_CHAT, 580)
    assert row is not None and row["status"] == Status.PUBLISHED
    assert len(fake_bot.send_video_calls) == 1
    assert fake_bot.send_video_calls[0]["supports_streaming"] is True
    assert fake_bot.send_document_calls == []


async def test_mp4_document_falls_back_once_on_clear_rejection(db, publisher, album_service,
                                                               auto_config, fake_bot):
    from handlers import Handlers

    handlers = Handlers(db, publisher, album_service, auto_config)
    handlers.bot = fake_bot
    fake_bot.video_error = BadRequest("VIDEO_CONTENT_TYPE_INVALID")

    update = attach_fake_bot(
        build_document_update(file_name="Suits S01E01.mp4", mime_type="video/mp4",
                           message_id=581),
        fake_bot,
    )
    await handlers.handle_media(update, _context())

    row = await db.get_item_by_source(SOURCE_CHAT, 581)
    assert row is not None and row["status"] == Status.PUBLISHED
    assert len(fake_bot.send_video_calls) == 1, "native attempt happens exactly once"
    assert len(fake_bot.send_document_calls) == 1, "then falls back to a document"


async def test_ambiguous_failure_never_falls_back(db, publisher, album_service,
                                                  auto_config, fake_bot):
    """A timeout must become uncertain, not a second send."""
    from handlers import Handlers

    handlers = Handlers(db, publisher, album_service, auto_config)
    handlers.bot = fake_bot
    fake_bot.video_error = TimedOut()

    update = attach_fake_bot(
        build_document_update(file_name="Suits S01E01.mp4", mime_type="video/mp4",
                           message_id=582),
        fake_bot,
    )
    await handlers.handle_media(update, _context())

    row = await db.get_item_by_source(SOURCE_CHAT, 582)
    assert row["status"] == Status.UNCERTAIN
    assert fake_bot.send_document_calls == [], "no blind retry after an ambiguous failure"


@pytest.mark.parametrize("builder,expected_type", [
    (build_photo_update, "photo"),
    (build_audio_update, "audio"),
    (build_animation_update, "animation"),
])
async def test_other_media_types_are_recorded(db, publisher, album_service, auto_config,
                                              fake_bot, builder, expected_type):
    from handlers import Handlers

    handlers = Handlers(db, publisher, album_service, auto_config)
    handlers.bot = fake_bot

    update = attach_fake_bot(builder(), fake_bot)
    await handlers.handle_media(update, _context())

    row = await db.get_item_by_source(SOURCE_CHAT, update.message.message_id)
    assert row is not None, f"{expected_type} upload must be recorded"
    assert row["telegram_media_type"] == expected_type
    assert row["status"] == Status.PUBLISHED


# --------------------------------------------------------------------------- #
# Duplicate protection for documents
# --------------------------------------------------------------------------- #


async def test_repeated_document_update_is_idempotent(db, publisher, album_service,
                                                      auto_config, fake_bot):
    from handlers import Handlers

    handlers = Handlers(db, publisher, album_service, auto_config)
    handlers.bot = fake_bot

    update = attach_fake_bot(build_document_update(), fake_bot)
    for _ in range(3):
        await handlers.handle_media(update, _context())

    counts = await db.counts_by_state("media_items")
    assert counts[Status.PUBLISHED] == 1
    assert len(fake_bot.send_document_calls) == 1, "document must be published once"


# --------------------------------------------------------------------------- #
# Rejections are logged with a reason
# --------------------------------------------------------------------------- #


async def test_rejection_logs_include_reason(db, publisher, album_service, config,
                                             fake_bot, caplog):
    import logging

    from handlers import Handlers

    handlers = Handlers(db, publisher, album_service, config)
    handlers.bot = fake_bot

    unauthorized = build_document_update(user_id=424242, message_id=590)
    wrong_chat = build_document_update(chat_id=-1009999999999, message_id=591)
    unconfigured_topic = build_document_update(thread_id=4242, message_id=592)
    bot_sender = build_document_update(message_id=593, is_bot=True)

    with caplog.at_level(logging.INFO, logger="handlers"):
        for update in (unauthorized, wrong_chat, unconfigured_topic, bot_sender):
            await handlers.handle_media(update, _context())

    reasons = {
        record.args[0]
        for record in caplog.records
        if "event=media-rejected" in record.msg and len(record.args) >= 1
    }
    assert {"unauthorized-user", "wrong-source-chat", "unconfigured-topic"} <= reasons
    assert (await db.counts_by_state("media_items"))[Status.PENDING] == 0


async def test_accepted_upload_is_logged(db, publisher, album_service, auto_config,
                                         fake_bot, caplog):
    import logging

    from handlers import Handlers

    handlers = Handlers(db, publisher, album_service, auto_config)
    handlers.bot = fake_bot

    with caplog.at_level(logging.INFO, logger="handlers"):
        await handlers.handle_media(attach_fake_bot(build_document_update(), fake_bot), _context())

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "event=media-received" in messages
    assert "telegram_type=document" in messages
    assert "extension=.mkv" in messages
    assert "event=media-created" in messages


# --------------------------------------------------------------------------- #
# Schema support
# --------------------------------------------------------------------------- #


async def test_schema_exposes_media_metadata_columns(db):
    columns = {row["name"] for row in await (await db._conn.execute(
        "PRAGMA table_info(media_items)"
    )).fetchall()}
    for name in ("telegram_media_type", "logical_media_type", "file_name",
                 "mime_type", "file_size"):
        assert name in columns
    assert db.schema_version == SCHEMA_VERSION


def _context():
    from tests.test_handlers import FakeContext

    return FakeContext()