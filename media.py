"""Centralised media detection and extraction.

Why this module exists
----------------------
python-telegram-bot does **not** model absent media as ``None``. In PTB 21.11.1
``Message.photo`` is an empty tuple ``()`` when there is no photo, while
``Message.video`` / ``animation`` / ``audio`` / ``document`` are ``None``.

Code that probes with ``getattr(message, "photo", None) is not None`` therefore
matches an empty tuple and misclassifies *every* message - including documents -
as a photo. That bug silently dropped document uploads (e.g. ``.mkv``) before a
database row was ever created.

Every presence check in this project must therefore use **truthiness**, never
``is not None``. All detection and extraction lives here so that filtering,
routing, storage and publishing can never disagree about a message.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

#: Message attributes inspected, in a stable order.
#: NOTE: check truthiness - see the module docstring.
TELEGRAM_MEDIA_ATTRS = ("video", "animation", "audio", "photo", "document")

#: Telegram media types this bot accepts.
SUPPORTED_TELEGRAM_TYPES = TELEGRAM_MEDIA_ATTRS

#: Document extensions that represent video content.
VIDEO_DOCUMENT_EXTENSIONS = frozenset({".mkv", ".mp4", ".m4v", ".mov", ".webm"})

#: Document extensions Telegram can usually play as a native video.
#: ``.mkv`` is deliberately excluded: Telegram's Bot API cannot transcode it, so
#: an MKV document is republished as a document.
NATIVE_VIDEO_DOCUMENT_EXTENSIONS = frozenset({".mp4", ".m4v", ".mov", ".webm"})


@dataclass(frozen=True)
class MediaInfo:
    """Everything the pipeline needs to know about one incoming media message."""

    telegram_type: str
    logical_type: str
    file_id: str
    file_unique_id: str = ""
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    file_size: Optional[int] = None
    extension: str = ""
    caption: Optional[str] = None
    source_message_id: int = 0
    source_chat_id: int = 0
    topic_id: Optional[int] = None
    media_group_id: Optional[str] = None

    @property
    def is_document(self) -> bool:
        return self.telegram_type == "document"

    @property
    def is_video_document(self) -> bool:
        return self.logical_type == "video_document"

    @property
    def prefers_native_video(self) -> bool:
        """Whether publishing should try ``send_video`` before ``send_document``."""
        return self.is_video_document and self.extension in NATIVE_VIDEO_DOCUMENT_EXTENSIONS


def detect_telegram_media_type(message) -> Optional[str]:
    """Return the Telegram media type of *message*, or ``None``.

    Uses truthiness because absent media is ``None`` *or* an empty tuple
    depending on the attribute.
    """
    if message is None:
        return None
    for attribute in TELEGRAM_MEDIA_ATTRS:
        if getattr(message, attribute, None):
            return attribute
    return None


def extension_of(file_name: Optional[str]) -> str:
    """Lower-cased extension including the dot, e.g. ``.mkv``."""
    if not file_name:
        return ""
    return os.path.splitext(file_name)[1].lower()


def is_video_document(file_name: Optional[str] = None, mime_type: Optional[str] = None) -> bool:
    """Classify a document as video-like.

    Extension is checked first and case-insensitively because Telegram frequently
    reports ``application/octet-stream`` for perfectly valid video files, so the
    MIME type alone cannot be trusted.
    """
    if extension_of(file_name) in VIDEO_DOCUMENT_EXTENSIONS:
        return True
    mime = (mime_type or "").strip().lower()
    return mime.startswith("video/")


def logical_type_for(telegram_type: str, file_name: Optional[str], mime_type: Optional[str]) -> str:
    """Map the Telegram representation onto a logical media category."""
    if telegram_type == "document":
        return "video_document" if is_video_document(file_name, mime_type) else "document"
    return telegram_type


def _photo_file_ids(message) -> tuple:
    """Return (file_id, file_unique_id) for the largest available photo size."""
    sizes = getattr(message, "photo", None) or ()
    if not sizes:
        return "", ""
    largest = sizes[-1]
    return getattr(largest, "file_id", "") or "", getattr(largest, "file_unique_id", "") or ""


def extract_media(message) -> Optional[MediaInfo]:
    """Build a :class:`MediaInfo` for *message*, or ``None`` if unsupported.

    Only metadata is read - the file is never downloaded, so a 1.2 GB MKV costs
    nothing beyond the Telegram API round trip that republishes it.
    """
    telegram_type = detect_telegram_media_type(message)
    if telegram_type is None:
        return None

    file_id = ""
    file_unique_id = ""
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    file_size: Optional[int] = None

    if telegram_type == "photo":
        file_id, file_unique_id = _photo_file_ids(message)
    else:
        media = getattr(message, telegram_type, None)
        file_id = getattr(media, "file_id", "") or ""
        file_unique_id = getattr(media, "file_unique_id", "") or ""
        file_name = getattr(media, "file_name", None)
        mime_type = getattr(media, "mime_type", None)
        raw_size = getattr(media, "file_size", None)
        file_size = int(raw_size) if isinstance(raw_size, (int, float)) else None

    if not file_id:
        logger.debug("media-missing-file-id telegram_type=%s", telegram_type)
        return None

    chat = getattr(message, "chat", None)
    return MediaInfo(
        telegram_type=telegram_type,
        logical_type=logical_type_for(telegram_type, file_name, mime_type),
        file_id=file_id,
        file_unique_id=file_unique_id,
        file_name=file_name,
        mime_type=mime_type,
        file_size=file_size,
        extension=extension_of(file_name),
        caption=getattr(message, "caption", None),
        source_message_id=int(getattr(message, "message_id", 0) or 0),
        source_chat_id=int(getattr(chat, "id", 0) or 0),
        topic_id=getattr(message, "message_thread_id", None),
        media_group_id=getattr(message, "media_group_id", None),
    )


# Backwards-compatible thin wrappers -------------------------------------------------
# These existed before this module; handlers still use the same names so the
# public surface stays stable.


def describe_media_type(message) -> Optional[str]:
    return detect_telegram_media_type(message)


def extract_file_id(message, media_type: str) -> Optional[str]:
    if media_type == "photo":
        return _photo_file_ids(message)[0] or None
    media = getattr(message, media_type, None)
    return getattr(media, "file_id", None) if media else None


def extract_file_unique_id(message, media_type: str) -> str:
    if media_type == "photo":
        return _photo_file_ids(message)[1]
    media = getattr(message, media_type, None)
    return (getattr(media, "file_unique_id", "") or "") if media else ""