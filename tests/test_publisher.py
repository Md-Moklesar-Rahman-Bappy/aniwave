"""Publisher correctness (Phase D / N items 5-7, 18-22)."""

from __future__ import annotations

import asyncio

import pytest
from telegram import (
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)
from telegram.error import BadRequest, Forbidden, InvalidToken, NetworkError, RetryAfter, TimedOut

from database import Status
from publisher import (
    ALBUM_MAX_ITEMS,
    ALBUM_MIN_ITEMS,
    AlbumValidationError,
    PublishOutcome,
    build_album_media,
    classify_telegram_error,
    validate_album_items,
)
from tests.conftest import ADMIN_ID, ONE_PIECE_TOPIC, SOURCE_CHAT
from tests.test_database import make_album, make_item


# --------------------------------------------------------------------------- #
# Plain text / target channel (Phase D.1-4)
# --------------------------------------------------------------------------- #


async def test_caption_sent_without_parse_mode(publisher, db, fake_bot):
    _, row_id = await make_item(db)
    outcome = await publisher.publish_item_row(row_id)
    assert outcome.result == "published"
    call = fake_bot.send_video_calls[0]
    assert "parse_mode" not in call
    assert call["parse_mode"] if "parse_mode" in call else True
    assert call["chat_id"] == "@aniwavebd"
    assert call["supports_streaming"] is True


async def test_target_channel_passed_as_string(publisher, db, fake_bot):
    _, row_id = await make_item(db)
    await publisher.publish_item_row(row_id)
    assert fake_bot.send_video_calls[0]["chat_id"] == "@aniwavebd"
    assert isinstance(fake_bot.send_video_calls[0]["chat_id"], str)


async def test_generated_caption_used_for_publication(publisher, db, fake_bot):
    _, row_id = await make_item(db)
    await publisher.publish_item_row(row_id)
    caption = fake_bot.send_video_calls[0]["caption"]
    assert "One Piece" in caption
    assert "Episode: 1165" in caption
    assert "Channel: @aniwavebd" in caption


async def test_hd_claim_configuration_respected(env, tmp_path, fake_bot, db):
    from config import AppConfig
    from publisher import Publisher

    values = dict(env)
    values["INCLUDE_HD_CLAIM"] = "true"
    hd_config = AppConfig.load(values)
    hd_publisher = Publisher(fake_bot, db, hd_config)
    _, row_id = await make_item(db)
    await hd_publisher.publish_item_row(row_id)
    assert "HD Quality" in fake_bot.send_video_calls[0]["caption"]


async def test_existing_file_id_is_reused_without_download(publisher, db, fake_bot):
    """The stored file_id is reused; the file is never downloaded."""
    _, row_id = await make_item(db, source_chat_id=SOURCE_CHAT, source_message_id=555)
    await publisher.publish_item_row(row_id)
    call = fake_bot.send_video_calls[0]
    assert call["video"] == "file-abc"
    assert fake_bot.copy_calls == [], "no re-upload via copy_message"


# --------------------------------------------------------------------------- #
# Album media construction (Phase D.5-10)
# --------------------------------------------------------------------------- #


def test_build_album_media_uses_concrete_classes():
    items = [
        {"media_type": "photo", "file_id": "a"},
        {"media_type": "video", "file_id": "b"},
        {"media_type": "animation", "file_id": "c"},
        {"media_type": "audio", "file_id": "d"},
        {"media_type": "document", "file_id": "e"},
    ]
    media = build_album_media(items)
    assert [type(m) for m in media] == [
        InputMediaPhoto,
        InputMediaVideo,
        InputMediaAnimation,
        InputMediaAudio,
        InputMediaDocument,
    ]


def test_generic_inputmedia_is_not_constructed():
    """Phase D.6: the abstract InputMedia class must never be instantiated."""
    from telegram import InputMedia

    for media_type in ("photo", "video", "animation", "audio", "document"):
        media = build_album_media([{"media_type": media_type, "file_id": "x"}])
        assert type(media[0]) is not InputMedia


def test_caption_applied_to_first_item_only():
    items = [
        {"media_type": "photo", "file_id": "a"},
        {"media_type": "photo", "file_id": "b"},
        {"media_type": "photo", "file_id": "c"},
    ]
    media = build_album_media(items, caption="episode caption")
    assert media[0].caption == "episode caption"
    assert media[1].caption is None
    assert media[2].caption is None


def test_album_media_order_is_preserved():
    items = [{"media_type": "photo", "file_id": f"f{i}"} for i in range(5)]
    media = build_album_media(items)
    assert [m.media for m in media] == [f"f{i}" for i in range(5)]


def test_build_album_media_rejects_unknown_type():
    with pytest.raises(AlbumValidationError):
        build_album_media([{"media_type": "sticker", "file_id": "x"}])


# --------------------------------------------------------------------------- #
# Album validation (Phase D.7-8)
# --------------------------------------------------------------------------- #


def part(media_type):
    return {"media_type": media_type, "file_id": "x"}


@pytest.mark.parametrize("types", [
    ["photo", "video"],
    ["photo", "photo"],
    ["video", "video", "photo"],
    ["audio", "audio"],
    ["document", "document"],
    ["animation", "animation"],
])
def test_valid_album_combinations(types):
    validate_album_items([part(t) for t in types])


@pytest.mark.parametrize("types", [
    ["audio", "video"],
    ["photo", "document"],
    ["audio", "document"],
    ["animation", "audio"],
])
def test_invalid_mixed_albums_are_rejected(types):
    with pytest.raises(AlbumValidationError) as excinfo:
        validate_album_items([part(t) for t in types])
    assert "separate messages" in str(excinfo.value)


def test_album_too_small_rejected():
    with pytest.raises(AlbumValidationError) as excinfo:
        validate_album_items([part("photo")])
    assert str(ALBUM_MIN_ITEMS) in str(excinfo.value)


def test_album_too_large_rejected():
    with pytest.raises(AlbumValidationError) as excinfo:
        validate_album_items([part("photo")] * (ALBUM_MAX_ITEMS + 1))
    assert str(ALBUM_MAX_ITEMS) in str(excinfo.value)


def test_album_with_unsupported_type_rejected():
    with pytest.raises(AlbumValidationError) as excinfo:
        validate_album_items([part("photo"), part("sticker")])
    assert "unsupported media" in str(excinfo.value).lower()


async def test_invalid_album_fails_before_any_telegram_call(publisher, db, fake_bot):
    """A rejected album must not claim success nor contact Telegram."""
    album_id, _ = await make_album(db)
    await db.add_album_item(album_id=album_id, source_chat_id=SOURCE_CHAT,
                            source_message_id=1, media_type="audio", file_id="a")
    await db.add_album_item(album_id=album_id, source_chat_id=SOURCE_CHAT,
                            source_message_id=2, media_type="video", file_id="b")
    await db.finish_album_collection(album_id, "5", "cap")

    outcome = await publisher.publish_album_row(album_id)
    assert outcome.result == "failed"
    assert fake_bot.album_calls == []
    assert (await db.get_album(album_id))["status"] == Status.FAILED


# --------------------------------------------------------------------------- #
# Error classification (Phase D / H)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("exc,uncertain", [
    (InvalidToken("bad token"), False),
    (Forbidden("no rights"), False),
    (BadRequest("message not found"), False),
    (RetryAfter(5), False),
    (TimedOut(), True),
    (NetworkError("connection reset"), True),
    (RuntimeError("unexpected"), True),
])
def test_error_classification(exc, uncertain):
    is_uncertain, message = classify_telegram_error(exc)
    assert is_uncertain is uncertain
    assert isinstance(message, str) and message


def test_bad_request_is_not_treated_as_uncertain():
    """BadRequest subclasses NetworkError in PTB; order must still be correct."""
    is_uncertain, _ = classify_telegram_error(BadRequest("nope"))
    assert is_uncertain is False


# --------------------------------------------------------------------------- #
# Publish outcomes (Phase G/H)
# --------------------------------------------------------------------------- #


async def test_successful_publish_stores_target_ids(publisher, db):
    _, row_id = await make_item(db)
    outcome = await publisher.publish_item_row(row_id)
    assert outcome.result == "published"
    assert outcome.target_message_ids
    row = await db.get_item(row_id)
    assert row["status"] == Status.PUBLISHED
    assert row["target_message_id"] == str(outcome.target_message_ids[0])


async def test_not_published_before_telegram_confirms(publisher, db, fake_bot):
    """A failure must not be recorded as published."""
    fake_bot.video_error = Forbidden("bot is not a member")
    _, row_id = await make_item(db)
    outcome = await publisher.publish_item_row(row_id)
    assert outcome.result == "failed"
    assert outcome.ok is False
    assert (await db.get_item(row_id))["status"] == Status.FAILED


async def test_clear_failure_allows_retry(publisher, db, fake_bot):
    fake_bot.video_error = RetryAfter(30)
    _, row_id = await make_item(db)
    await publisher.publish_item_row(row_id)
    assert (await db.get_item(row_id))["status"] == Status.FAILED

    fake_bot.video_error = None
    outcome = await publisher.publish_item_row(row_id)
    assert outcome.result == "published"


async def test_timeout_produces_uncertain_not_failed(publisher, db, fake_bot):
    fake_bot.video_error = TimedOut()
    _, row_id = await make_item(db)
    outcome = await publisher.publish_item_row(row_id)
    assert outcome.result == "uncertain"
    assert outcome.needs_review is True
    row = await db.get_item(row_id)
    assert row["status"] == Status.UNCERTAIN
    assert row["uncertain"] == 1


async def test_uncertain_record_is_not_retried_automatically(publisher, db, fake_bot):
    fake_bot.video_error = TimedOut()
    _, row_id = await make_item(db)
    await publisher.publish_item_row(row_id)
    assert (await db.get_item(row_id))["status"] == Status.UNCERTAIN

    fake_bot.copy_error = None
    calls_before = len(fake_bot.send_video_calls)
    outcome = await publisher.publish_item_row(row_id)
    assert outcome.result == "refused"
    assert "uncertain" in outcome.reason
    assert len(fake_bot.send_video_calls) == calls_before


async def test_publish_refused_without_episode(publisher, db, fake_bot):
    _, row_id = await make_item(db, episode_number=None)
    outcome = await publisher.publish_item_row(row_id)
    assert outcome.result == "refused"
    assert "episode" in outcome.reason.lower()
    assert fake_bot.send_video_calls == []


async def test_publish_refused_for_missing_record(publisher):
    outcome = await publisher.publish_item_row(999999)
    assert outcome.result == "refused"
    assert "not found" in outcome.reason


async def test_publish_refused_from_published_state(publisher, db):
    _, row_id = await make_item(db)
    await publisher.publish_item_row(row_id)
    outcome = await publisher.publish_item_row(row_id)
    assert outcome.result == "refused"


async def test_concurrent_publish_sends_once(publisher, db, fake_bot):
    _, row_id = await make_item(db)
    outcomes = await asyncio.gather(
        *[publisher.publish_item_row(row_id) for _ in range(10)]
    )
    assert len(fake_bot.send_video_calls) == 1
    assert sum(1 for o in outcomes if o.result == "published") == 1


async def test_concurrent_album_publish_sends_once(publisher, db, fake_bot):
    album_id, _ = await make_album(db)
    for message_id in (1, 2, 3):
        await db.add_album_item(album_id=album_id, source_chat_id=SOURCE_CHAT,
                                source_message_id=message_id, media_type="photo",
                                file_id=f"f{message_id}")
    await db.finish_album_collection(album_id, "1165", "cap")

    outcomes = await asyncio.gather(
        *[publisher.publish_album_row(album_id) for _ in range(6)]
    )
    assert len(fake_bot.album_calls) == 1
    assert sum(1 for o in outcomes if o.result == "published") == 1


async def test_album_publish_stores_all_target_ids(publisher, db, fake_bot):
    album_id, _ = await make_album(db)
    for message_id in (1, 2, 3):
        await db.add_album_item(album_id=album_id, source_chat_id=SOURCE_CHAT,
                                source_message_id=message_id, media_type="photo",
                                file_id=f"f{message_id}")
    await db.finish_album_collection(album_id, "1165", "cap")
    outcome = await publisher.publish_album_row(album_id)
    assert outcome.result == "published"
    assert len(outcome.target_message_ids) == 3
    album = await db.get_album(album_id)
    assert album["status"] == Status.PUBLISHED


async def test_album_timeout_produces_uncertain(publisher, db, fake_bot):
    album_id, _ = await make_album(db)
    for message_id in (1, 2):
        await db.add_album_item(album_id=album_id, source_chat_id=SOURCE_CHAT,
                                source_message_id=message_id, media_type="photo", file_id="f")
    await db.finish_album_collection(album_id, "1165", "cap")
    fake_bot.album_error = NetworkError("reset")
    outcome = await publisher.publish_album_row(album_id)
    assert outcome.result == "uncertain"
    assert (await db.get_album(album_id))["status"] == Status.UNCERTAIN


async def test_album_refused_from_collecting_state(publisher, db, fake_bot):
    album_id, _ = await make_album(db)
    outcome = await publisher.publish_album_row(album_id)
    assert outcome.result == "refused"
    assert fake_bot.album_calls == []


async def test_album_refused_without_episode(publisher, db, fake_bot):
    album_id, _ = await make_album(db)
    for message_id in (1, 2):
        await db.add_album_item(album_id=album_id, source_chat_id=SOURCE_CHAT,
                                source_message_id=message_id, media_type="photo", file_id="f")
    await db.finish_album_collection(album_id, None, "")
    outcome = await publisher.publish_album_row(album_id)
    assert outcome.result == "refused"
    assert fake_bot.album_calls == []


async def test_publish_outcome_defaults():
    outcome = PublishOutcome(result="refused")
    assert outcome.ok is False
    assert outcome.needs_review is False
    assert outcome.target_message_ids == ()