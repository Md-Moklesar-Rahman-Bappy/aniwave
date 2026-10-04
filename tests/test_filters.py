"""Media filter behaviour (Phase C / N items 3-4).

These are behaviour tests: each case builds a message-shaped object and asserts
whether the filter accepts it. Nothing here asserts ``filters.ALL``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telegram.ext import filters

from handlers import SUPPORTED_MEDIA, describe_media_type, extract_file_id, extract_file_unique_id


def make_message(**attrs):
    """Build a message stub with only the attributes a filter inspects."""
    base = {
        "video": None,
        "animation": None,
        "audio": None,
        "photo": None,
        "document": None,
        "media_group_id": None,
        "text": None,
        "sticker": None,
        "contact": None,
        "location": None,
        "poll": None,
        "caption": None,
    }
    base.update(attrs)
    return SimpleNamespace(**base)


def make_update(message=None, **extra):
    """Build an update stub exposing every attribute PTB's filter inspects."""
    attrs = {
        "channel_post": None,
        "message": message,
        "edited_channel_post": None,
        "edited_message": None,
        "business_message": None,
        "edited_business_message": None,
        "callback_query": None,
        "effective_message": message,
    }
    attrs.update(extra)
    return SimpleNamespace(**attrs)


# --------------------------------------------------------------------------- #
# Acceptance
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("attribute", ["video", "animation", "audio", "photo", "document"])
def test_supported_media_is_accepted(attribute):
    message = make_message(**{attribute: object()})
    assert SUPPORTED_MEDIA.filter(message) is True


def test_album_part_is_accepted():
    """Album parts carry a media_group_id and must reach album collection."""
    message = make_message(photo=[object()], media_group_id="grp-1")
    assert SUPPORTED_MEDIA.filter(message) is True


def test_album_part_without_recognised_type_still_accepted():
    """media_group_id alone routes the message into the album collector."""
    message = make_message(media_group_id="grp-1")
    assert SUPPORTED_MEDIA.filter(message) is True


# --------------------------------------------------------------------------- #
# Rejection
# --------------------------------------------------------------------------- #


def test_plain_text_is_rejected():
    assert SUPPORTED_MEDIA.filter(make_message(text="EP 1165")) is False


def test_command_text_is_rejected():
    assert SUPPORTED_MEDIA.filter(make_message(text="/status")) is False


def test_empty_message_is_rejected():
    assert SUPPORTED_MEDIA.filter(make_message()) is False


def test_none_message_is_rejected():
    assert SUPPORTED_MEDIA.filter(None) is False


@pytest.mark.parametrize("attribute", ["sticker", "contact", "location", "poll"])
def test_unsupported_content_types_are_rejected(attribute):
    assert SUPPORTED_MEDIA.filter(make_message(**{attribute: object()})) is False


def test_voice_and_video_note_are_rejected():
    message = make_message()
    message.voice = object()
    message.video_note = object()
    assert SUPPORTED_MEDIA.filter(message) is False


# --------------------------------------------------------------------------- #
# PTB integration
# --------------------------------------------------------------------------- #


def test_filter_is_a_ptb_base_filter():
    assert isinstance(SUPPORTED_MEDIA, filters.BaseFilter)


def test_filter_resolves_message_from_an_update():
    update = make_update(make_message(video=object()))
    assert SUPPORTED_MEDIA.check_update(update) is True


def test_filter_rejects_update_without_media():
    update = make_update(make_message(text="hello"))
    assert SUPPORTED_MEDIA.check_update(update) is False


def test_filter_rejects_update_without_a_message():
    assert SUPPORTED_MEDIA.check_update(make_update(None)) is False


def test_filter_does_not_match_callback_queries():
    update = make_update(None, callback_query=object())
    assert SUPPORTED_MEDIA.check_update(update) is False


def test_filter_differs_from_filters_all():
    """Regression guard: the media filter must not be filters.ALL."""
    assert SUPPORTED_MEDIA is not filters.ALL
    assert SUPPORTED_MEDIA.filter(make_message(text="plain text")) is False


def test_document_filter_is_not_ptb_document_class():
    """PTB 21 filters.Document is not a BaseFilter, which is why we subclass."""
    assert not issubclass(filters.Document, filters.BaseFilter)


# --------------------------------------------------------------------------- #
# Media extraction helpers
# --------------------------------------------------------------------------- #


def test_describe_media_type():
    assert describe_media_type(make_message(video=object())) == "video"
    assert describe_media_type(make_message(document=object())) == "document"
    assert describe_media_type(make_message()) is None


def test_extract_file_id_uses_largest_photo_size():
    sizes = [
        SimpleNamespace(file_id="small", file_unique_id="us"),
        SimpleNamespace(file_id="large", file_unique_id="ul"),
    ]
    message = make_message(photo=sizes)
    assert extract_file_id(message, "photo") == "large"
    assert extract_file_unique_id(message, "photo") == "ul"


def test_extract_file_id_for_video():
    message = make_message(video=SimpleNamespace(file_id="v1", file_unique_id="uv"))
    assert extract_file_id(message, "video") == "v1"
    assert extract_file_unique_id(message, "video") == "uv"


def test_extract_file_id_returns_none_without_media():
    assert extract_file_id(make_message(), "photo") is None