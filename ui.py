"""Callback-data encoding and admin-facing message builders.

All text here is **plain text** - no MarkdownV2 - so reserved characters such as
``.``, ``-`` and ``@`` can never trigger a ``BadRequest``.

This module deliberately has no dependency on handlers/albums so both can use
it without a circular import.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

#: ``aw:<action>:<kind>:<id>`` - compact, validated, and never able to address a
#: table/row outside the two known record kinds.
CALLBACK_PATTERN = re.compile(r"^aw:(?P<act>[a-z]{2}):(?P<kind>[ma]):(?P<id>[0-9]{1,12})$")

KIND_ITEM = "m"
KIND_ALBUM = "a"

ACT_PUBLISH = "p"
ACT_EDIT = "e"
ACT_CANCEL = "c"
ACT_UNCERTAIN_PUBLISHED = "up"
ACT_UNCERTAIN_ABSENT = "uf"
ACT_CONFIRM_ABSENT = "uc"

_PREFIX = "aw"


def encode_callback(action: str, kind: str, row_id: int) -> str:
    """Build callback data for a row."""
    return f"{_PREFIX}:{action}:{kind}:{int(row_id)}"


def decode_callback(data: Optional[str]) -> Optional[Tuple[str, str, int]]:
    """Parse callback data. Returns ``None`` for anything unexpected."""
    if not data or not isinstance(data, str):
        return None
    match = CALLBACK_PATTERN.match(data)
    if not match:
        return None
    try:
        row_id = int(match.group("id"))
    except ValueError:  # pragma: no cover - regex already guarantees digits
        return None
    return match.group("act"), match.group("kind"), row_id


def parse_target(reference: Optional[str]) -> Optional[Tuple[str, int]]:
    """Parse a ``/retry`` target such as ``12``, ``m:12`` or ``a:12``."""
    if not reference:
        return None
    text = reference.strip().lower()
    if text.isdigit():
        return KIND_ITEM, int(text)
    if ":" in text:
        kind, _, raw = text.partition(":")
        kind = kind.strip()
        raw = raw.strip()
        if kind in (KIND_ITEM, KIND_ALBUM) and raw.isdigit():
            return kind, int(raw)
    return None


# --------------------------------------------------------------------------- #
# Keyboards
# --------------------------------------------------------------------------- #


def build_preview_keyboard(kind: str, row_id: int) -> InlineKeyboardMarkup:
    """Publish / Edit Episode / Cancel for a pending record."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Publish", callback_data=encode_callback(ACT_PUBLISH, kind, row_id))],
            [
                InlineKeyboardButton("Edit Episode", callback_data=encode_callback(ACT_EDIT, kind, row_id)),
                InlineKeyboardButton("Cancel", callback_data=encode_callback(ACT_CANCEL, kind, row_id)),
            ],
        ]
    )


def build_uncertain_keyboard(kind: str, row_id: int) -> InlineKeyboardMarkup:
    """Resolution controls for an interrupted (uncertain) publish."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Already published",
                    callback_data=encode_callback(ACT_UNCERTAIN_PUBLISHED, kind, row_id),
                )
            ],
            [
                InlineKeyboardButton(
                    "Not published",
                    callback_data=encode_callback(ACT_UNCERTAIN_ABSENT, kind, row_id),
                )
            ],
        ]
    )


def build_confirm_absent_keyboard(kind: str, row_id: int) -> InlineKeyboardMarkup:
    """Second confirmation before an uncertain record becomes retryable."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Yes, it is not published - allow retry",
                    callback_data=encode_callback(ACT_CONFIRM_ABSENT, kind, row_id),
                )
            ],
            [
                InlineKeyboardButton(
                    "No, keep as uncertain",
                    callback_data=encode_callback(ACT_UNCERTAIN_PUBLISHED, kind, row_id),
                )
            ],
        ]
    )


# --------------------------------------------------------------------------- #
# Text builders (plain text)
# --------------------------------------------------------------------------- #

_MAX_TEXT = 3500


def preview_text(
    *,
    topic_label: str,
    anime_title: str,
    episode_number: Optional[str],
    media_type: str,
    kind: str,
    row_id: int,
    album_items: Optional[int] = None,
) -> str:
    lines = [
        "Pending approval",
        "",
        f"Topic: {topic_label}",
        f"Anime: {anime_title or 'unknown'}",
        f"Episode: {episode_number or 'NOT SET'}",
        f"Type: {'album' if kind == KIND_ALBUM else media_type}",
    ]
    if album_items:
        lines.append(f"Album parts: {album_items}")
    lines += ["", f"Record: {kind}:{row_id}", "", "Use the buttons below to decide."]
    return "\n".join(lines)


def published_text(*, topic_label: str, anime_title: str, episode_number: str, target_ids: Tuple[int, ...]) -> str:
    shown = ", ".join(str(t) for t in target_ids) if target_ids else "unknown"
    return "\n".join(
        [
            "Published successfully",
            "",
            f"Topic: {topic_label}",
            f"Anime: {anime_title or 'unknown'}",
            f"Episode: {episode_number}",
            f"Channel message id(s): {shown}",
        ]
    )


def failed_text(reason: str) -> str:
    return "\n".join(
        [
            "Publishing failed.",
            "",
            f"Reason: {reason}",
            "",
            "Nothing was published. Fix the cause, then use /retry or press Publish again.",
        ]
    )


def uncertain_text(kind: str, row_id: int, reason: str) -> str:
    return "\n".join(
        [
            "Publication outcome UNKNOWN - manual check required.",
            "",
            f"Record: {kind}:{row_id}",
            f"Reason: {reason}",
            "",
            "The bot stopped while publishing, so the post may or may not exist in the channel.",
            "Check the channel, then choose one of the buttons below.",
            "It will NOT be retried automatically.",
        ]
    )


def no_episode_hint() -> str:
    """Guidance appended when an episode number could not be determined."""
    return (
        "No episode number was detected.\n"
        "Accepted caption forms: EP 1165, Episode 25, 12.5, or just 1165\n"
        "Use Edit Episode to set it now, or fix the caption and use /retry."
    )


def needs_episode_text(topic_label: str) -> str:
    return "\n".join(
        [
            "Cannot publish yet: no episode number was found.",
            "",
            f"Topic: {topic_label}",
            "Accepted caption forms: EP 1165, Episode 25, 12.5, or just 1165",
            "",
            "Press Edit Episode to set it manually, or fix the caption and use /retry.",
        ]
    )


def status_text(*, config, counts_items, counts_albums, database_ok: bool) -> str:
    mode = "automatic" if config.auto_publish else "approval"
    lines = [
        "Bot status",
        "",
        f"Version: {_safe_version()}",
        "Running: yes",
        f"Source group id: {config.source_group_id}",
        f"Target channel: {config.target_channel}",
        f"Mode: {mode}",
        f"HD claim in caption: {'yes' if config.include_hd_claim else 'no'}",
        f"Album quiet period: {config.album_quiet_seconds}s",
        f"Database: {'OK' if database_ok else 'UNAVAILABLE'}",
        "",
        "Configured topics:",
    ]
    for topic in config.topics:
        lines.append(f"  {topic.emoji} {topic.title} -> topic id {topic.topic_id}")
    lines += ["", "Single uploads:"]
    lines += [f"  {state}: {counts_items.get(state, 0)}" for state in sorted(counts_items)]
    lines += ["", "Albums:"]
    lines += [f"  {state}: {counts_albums.get(state, 0)}" for state in sorted(counts_albums)]
    text = "\n".join(lines)
    return text[:_MAX_TEXT]


def _safe_version() -> str:
    from config import APP_VERSION

    return APP_VERSION


def debug_text(*, update, config) -> str:
    """Report safe diagnostics only - never secrets or filesystem paths."""
    chat = getattr(update, "effective_chat", None)
    user = getattr(update, "effective_user", None)
    message = getattr(update, "effective_message", None)

    chat_id = getattr(chat, "id", None)
    chat_type = getattr(chat, "type", None)
    thread_id = getattr(message, "message_thread_id", None) if message else None
    media_type, media_group_id = _describe_media(message)

    lines = [
        "Debug",
        "",
        f"App version: {_safe_version()}",
        f"Chat id: {chat_id}",
        f"Chat type: {chat_type}",
        f"Message thread id: {thread_id}",
        f"User id: {getattr(user, 'id', None)}",
        f"Is configured source chat: {'yes' if chat_id == config.source_group_id else 'no'}",
        f"Is admin: {'yes' if config.is_admin(getattr(user, 'id', None)) else 'no'}",
        f"Configured topic: {_configured_topic_label(config, thread_id)}",
        f"Publishing mode: {'automatic' if config.auto_publish else 'approval'}",
        f"Media type: {media_type}",
        f"Media group id: {media_group_id}",
    ]
    return "\n".join(lines)


def _configured_topic_label(config, thread_id) -> str:
    topic = config.topic_for(thread_id)
    return f"{topic.emoji} {topic.title}" if topic else "not configured"


def _describe_media(message) -> Tuple[str, Optional[str]]:
    if message is None:
        return "none", None
    for attribute, label in (
        ("video", "video"),
        ("animation", "animation"),
        ("audio", "audio"),
        ("document", "document"),
        ("photo", "photo"),
    ):
        if getattr(message, attribute, None) is not None:
            return label, getattr(message, "media_group_id", None)
    return "none", getattr(message, "media_group_id", None)


def chat_id_text(*, chat, user) -> str:
    return "\n".join(
        [
            "Chat information",
            "",
            f"Chat id: {getattr(chat, 'id', None)}",
            f"Chat type: {getattr(chat, 'type', None)}",
            f"Chat title: {getattr(chat, 'title', None)}",
            f"Your user id: {getattr(user, 'id', None)}",
        ]
    )


def topic_id_text(*, message, config) -> str:
    thread_id = getattr(message, "message_thread_id", None)
    lines = [
        "Topic information",
        "",
        f"Message thread id: {thread_id}",
        f"Configured here: {_configured_topic_label(config, thread_id)}",
        "",
        "Put the Message thread id into the matching variable in your .env file.",
    ]
    return "\n".join(lines)


def help_text(*, config) -> str:
    mode = (
        "Uploads publish automatically."
        if config.auto_publish
        else "Uploads are held for approval with Publish / Edit Episode / Cancel buttons."
    )
    return "\n".join(
        [
            "AniWave publishing bot",
            "",
            "How to publish",
            f"1. Open the configured anime topic in the source group ({mode})",
            "2. Upload video, animation, audio, photo, a document, or an album.",
            "3. Put the episode in the caption: EP 1165, Episode 25, 12.5, or 1165.",
            "4. Approve with the Publish button.",
            "",
            "Commands",
            "/start - status and configuration overview",
            "/help - this message",
            "/status - record counters and database health",
            "/debug - safe diagnostics for the current chat/topic",
            "/chatid - numeric chat id",
            "/topicid - numeric topic id for this topic",
            "/retry [m|a]<id> - retry a failed record",
            "/uncertain - list interrupted publications needing review",
            "/cancel - abandon an Edit Episode session",
            "",
            "Supported topics",
        ]
        + [f"  {topic.emoji} {topic.title} -> {topic.topic_id}" for topic in config.topics]
    )