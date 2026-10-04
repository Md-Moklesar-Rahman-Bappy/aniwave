"""Publication layer.

Responsibilities
----------------
* Build plain-text captions (never MarkdownV2 - the generated caption contains
  reserved characters that previously caused ``BadRequest``).
* Construct the correct concrete PTB input-media classes for albums.
* Reject album combinations Telegram cannot represent *before* claiming success.
* Classify Telegram failures as **clear** (safe to retry) or **uncertain**
  (outcome unknown, must never be retried automatically).
* Gate every send behind an atomic compare-and-set state transition.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from telegram import (
    Bot,
    InputMedia,
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)
from telegram.error import (
    BadRequest,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)

from caption import fit_caption, generate_caption
from config import AppConfig, get_config
from database import (
    PUBLISHABLE_STATES,
    SUPPORTED_MEDIA_TYPES,
    Database,
)

logger = logging.getLogger(__name__)

#: Telegram accepts between 2 and 10 items in one media group.
ALBUM_MIN_ITEMS = 2
ALBUM_MAX_ITEMS = 10

#: Types that Telegram renders as a real album and allows to be mixed.
_MIXABLE_ALBUM_TYPES = frozenset({"photo", "video"})

_INPUT_MEDIA_CLASSES = {
    "photo": InputMediaPhoto,
    "video": InputMediaVideo,
    "animation": InputMediaAnimation,
    "audio": InputMediaAudio,
    "document": InputMediaDocument,
}


class AlbumValidationError(ValueError):
    """Raised when an album cannot be represented by ``sendMediaGroup``."""


class PublishRefused(RuntimeError):
    """Raised internally when a row is not in a publishable state."""


@dataclass
class PublishOutcome:
    """Result of a publication attempt."""

    result: str  # published | failed | uncertain | refused
    target_message_ids: Tuple[int, ...] = ()
    final_caption: str = ""
    error: str = ""
    reason: str = ""
    row_id: Optional[int] = None
    kind: str = "item"

    @property
    def ok(self) -> bool:
        return self.result == "published"

    @property
    def needs_review(self) -> bool:
        return self.result == "uncertain"


def validate_album_items(items: Sequence[Dict[str, Any]]) -> None:
    """Validate an album before any Telegram call is attempted.

    A media group always needs 2-10 items. Telegram does not allow photos or
    videos to share an album with other media types, and non-album types must be
    homogeneous: either the whole album is photos/videos, or every item shares
    one other type.
    """
    count = len(items)
    if count < ALBUM_MIN_ITEMS:
        raise AlbumValidationError(
            f"an album needs at least {ALBUM_MIN_ITEMS} items (got {count})"
        )
    if count > ALBUM_MAX_ITEMS:
        raise AlbumValidationError(
            f"Telegram allows at most {ALBUM_MAX_ITEMS} items per album (got {count})"
        )

    types = [str(item.get("media_type", "")) for item in items]
    unsupported = sorted({t for t in types if t not in SUPPORTED_MEDIA_TYPES})
    if unsupported:
        raise AlbumValidationError(
            f"unsupported media type(s) in album: {', '.join(unsupported)}"
        )

    has_mixable = any(t in _MIXABLE_ALBUM_TYPES for t in types)
    others = {t for t in types if t not in _MIXABLE_ALBUM_TYPES}

    if has_mixable and others:
        raise AlbumValidationError(
            "photos/videos cannot share an album with "
            f"{', '.join(sorted(others))}; send them as separate messages"
        )
    if others and len(others) > 1:
        raise AlbumValidationError(
            f"albums mixing {', '.join(sorted(others))} cannot be sent as one album; "
            "send them as separate messages"
        )


def build_album_media(
    items: Sequence[Dict[str, Any]], *, caption: Optional[str] = None
) -> List[InputMedia]:
    """Build concrete PTB input-media objects, preserving order.

    The generated caption is applied to the **first** item only; Telegram
    displays one album caption and duplicates would be noise.
    """
    media: List[InputMedia] = []
    for index, item in enumerate(items):
        media_type = str(item["media_type"])
        cls = _INPUT_MEDIA_CLASSES.get(media_type)
        if cls is None:
            raise AlbumValidationError(f"unsupported media type {media_type!r}")
        kwargs: Dict[str, Any] = {"media": item["file_id"]}
        if index == 0 and caption:
            kwargs["caption"] = fit_caption(caption)
        media.append(cls(**kwargs))
    return media


def classify_telegram_error(exc: BaseException) -> Tuple[bool, str]:
    """Return ``(is_uncertain, safe_message)`` for an exception.

    ``is_uncertain`` means the request may already have been delivered, so the
    record must become ``uncertain`` instead of ``failed``: retrying a
    ``failed`` record is safe, retrying an ``uncertain`` one is not.

    Order matters: in python-telegram-bot ``BadRequest`` *subclasses*
    ``NetworkError``, so concrete rejection reasons are tested first.
    """
    if isinstance(exc, InvalidToken):
        return False, "bot token rejected by Telegram"
    if isinstance(exc, Forbidden):
        return False, "bot lacks permission in the target chat"
    if isinstance(exc, BadRequest):
        return False, f"Telegram rejected the request: {exc}"
    if isinstance(exc, RetryAfter):
        return False, "rate limited by Telegram; nothing was published"
    if isinstance(exc, (TimedOut, NetworkError)):
        return True, "network timeout while publishing; delivery unknown"
    if isinstance(exc, TelegramError):
        return False, f"Telegram error: {type(exc).__name__}"
    return True, f"unexpected error during publish: {type(exc).__name__}"


class Publisher:
    """Publishes rows from SQLite to the target channel."""

    def __init__(self, bot: Bot, database: Database, config: Optional[AppConfig] = None) -> None:
        self.bot = bot
        self.db = database
        self.config = config or get_config()

    # ------------------------------------------------------------------ #
    # Caption helpers
    # ------------------------------------------------------------------ #

    def build_caption(self, row: Dict[str, Any]) -> str:
        """Generate the final plain-text caption for a row."""
        episode = row.get("episode_number")
        if not episode:
            return ""
        return generate_caption(
            emoji=row.get("emoji") or "",
            anime_title=row.get("anime_title") or "",
            episode_number=str(episode),
            include_hd_claim=self.config.include_hd_claim,
            channel=self._channel_label(),
        )

    def _channel_label(self) -> str:
        target = self.config.target_channel
        return target if target.startswith("@") else f"id {target}"

    # ------------------------------------------------------------------ #
    # Low-level Telegram calls
    # ------------------------------------------------------------------ #

    async def send_single_media(
        self,
        *,
        source_chat_id: int,
        source_message_id: int,
        target_chat_id: str,
        caption: Optional[str],
    ) -> Tuple[Tuple[int, ...], Optional[BaseException]]:
        """Copy a single message, letting Telegram reuse the stored file.

        ``copy_message`` is used instead of a download/re-upload so no bytes
        travel through this machine.
        """
        kwargs: Dict[str, Any] = {}
        if caption:
            kwargs["caption"] = fit_caption(caption)
        try:
            result = await self.bot.copy_message(
                chat_id=target_chat_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id,
                **kwargs,
            )
            return (int(result.message_id),), None
        except Exception as exc:  # noqa: BLE001 - classified below
            return (), exc

    async def send_album_media(
        self, *, target_chat_id: str, media: Sequence[InputMedia]
    ) -> Tuple[Tuple[int, ...], Optional[BaseException]]:
        try:
            sent = await self.bot.send_media_group(chat_id=target_chat_id, media=list(media))
            return tuple(int(m.message_id) for m in sent), None
        except Exception as exc:  # noqa: BLE001 - classified below
            return (), exc

    # ------------------------------------------------------------------ #
    # Orchestration
    # ------------------------------------------------------------------ #

    async def publish_item_row(self, row_id: int) -> PublishOutcome:
        """Publish one single-media row, gated by an atomic state transition."""
        row = await self.db.get_item(row_id)
        if row is None:
            return PublishOutcome(
                result="refused", reason="record not found", row_id=row_id, kind="item"
            )
        if row["status"] not in PUBLISHABLE_STATES:
            return PublishOutcome(
                result="refused",
                reason=f"cannot publish from state {row['status']!r}",
                row_id=row_id,
                kind="item",
            )
        if not row.get("episode_number"):
            return PublishOutcome(
                result="refused",
                reason="no episode number; use Edit Episode or /retry after adding a caption",
                row_id=row_id,
                kind="item",
            )

        caption = self.build_caption(row)
        target = self.config.target_channel

        claimed = await self.db.claim_item_for_publishing(row_id)
        if not claimed:
            logger.info(
                "publish-gate-closed kind=item row=%s", row_id
            )
            return PublishOutcome(
                result="refused", reason="state changed before publish", row_id=row_id, kind="item"
            )

        logger.info(
            "publish-attempt kind=item row=%s chat=%s message=%s mode=%s",
            row_id, row["source_chat_id"], row["source_message_id"],
            "auto" if self.config.auto_publish else "approval",
        )

        ids, exc = await self.send_single_media(
            source_chat_id=int(row["source_chat_id"]),
            source_message_id=int(row["source_message_id"]),
            target_chat_id=target,
            caption=caption,
        )
        return await self._finalize_item(row, ids, exc, caption)

    async def publish_album_row(self, album_id: int) -> PublishOutcome:
        """Publish one album row, gated by an atomic state transition."""
        album = await self.db.get_album(album_id)
        if album is None:
            return PublishOutcome(
                result="refused", reason="album not found", row_id=album_id, kind="album"
            )
        if album["status"] not in PUBLISHABLE_STATES:
            return PublishOutcome(
                result="refused",
                reason=f"cannot publish from state {album['status']!r}",
                row_id=album_id,
                kind="album",
            )

        items = await self.db.get_album_items(album_id)
        if not album.get("episode_number"):
            return PublishOutcome(
                result="refused",
                reason="no episode number; use Edit Episode or /retry after adding a caption",
                row_id=album_id,
                kind="album",
            )

        try:
            validate_album_items(items)
        except AlbumValidationError as exc:
            # Refuse before claiming: nothing was sent, so a plain failure is
            # recorded and the admin can act on the explanation.
            await self.db.fail_album_if_publishable(album_id, str(exc))
            logger.warning("album-rejected album=%s reason=%s", album_id, exc)
            return PublishOutcome(
                result="failed", reason=str(exc), error=str(exc), row_id=album_id, kind="album"
            )

        caption = album.get("final_caption") or self.build_caption(album)
        try:
            media = build_album_media(items, caption=caption)
        except AlbumValidationError as exc:  # pragma: no cover - validated above
            await self.db.fail_album_if_publishable(album_id, str(exc))
            return PublishOutcome(
                result="failed", reason=str(exc), error=str(exc), row_id=album_id, kind="album"
            )
        target = self.config.target_channel

        claimed = await self.db.claim_album_for_publishing(album_id)
        if not claimed:
            logger.info("publish-gate-closed kind=album row=%s", album_id)
            return PublishOutcome(
                result="refused", reason="state changed before publish", row_id=album_id, kind="album"
            )

        logger.info(
            "publish-attempt kind=album row=%s chat=%s group=%s items=%d",
            album_id, album["source_chat_id"], album["media_group_id"], len(items),
        )

        ids, exc = await self.send_album_media(target_chat_id=target, media=media)
        return await self._finalize_album(album, ids, exc, caption)

    # ------------------------------------------------------------------ #
    # Finalisation
    # ------------------------------------------------------------------ #

    async def _finalize_item(
        self,
        row: Dict[str, Any],
        ids: Tuple[int, ...],
        exc: Optional[BaseException],
        caption: str,
    ) -> PublishOutcome:
        row_id = int(row["id"])
        if ids and exc is None:
            ok = await self.db.finish_item_published(
                row_id,
                target_chat_id=self.config.target_channel,
                target_message_ids=ids,
                final_caption=caption,
            )
            logger.info(
                "publish-succeeded kind=item row=%s targets=%s", row_id, list(ids)
            )
            return PublishOutcome(
                result="published" if ok else "failed",
                target_message_ids=ids,
                final_caption=caption,
                row_id=row_id,
                kind="item",
                reason="" if ok else "state changed during publish",
            )

        assert exc is not None
        uncertain, message = classify_telegram_error(exc)
        if uncertain:
            await self.db.mark_item_uncertain(row_id, message)
            logger.error("publish-uncertain kind=item row=%s reason=%s", row_id, message)
            return PublishOutcome(
                result="uncertain", error=message, row_id=row_id, kind="item",
                reason="inspect the channel before retrying",
            )
        await self.db.finish_item_failed(row_id, message)
        logger.warning("publish-failed kind=item row=%s reason=%s", row_id, message)
        return PublishOutcome(result="failed", error=message, row_id=row_id, kind="item")

    async def _finalize_album(
        self,
        album: Dict[str, Any],
        ids: Tuple[int, ...],
        exc: Optional[BaseException],
        caption: str,
    ) -> PublishOutcome:
        album_id = int(album["id"])
        if ids and exc is None:
            ok = await self.db.finish_album_published(
                album_id,
                target_chat_id=self.config.target_channel,
                target_message_ids=ids,
                final_caption=caption,
            )
            logger.info(
                "publish-succeeded kind=album row=%s targets=%s", album_id, list(ids)
            )
            return PublishOutcome(
                result="published" if ok else "failed",
                target_message_ids=ids,
                final_caption=caption,
                row_id=album_id,
                kind="album",
                reason="" if ok else "state changed during publish",
            )

        assert exc is not None
        uncertain, message = classify_telegram_error(exc)
        if uncertain:
            await self.db.mark_album_uncertain(album_id, message)
            logger.error("publish-uncertain kind=album row=%s reason=%s", album_id, message)
            return PublishOutcome(
                result="uncertain", error=message, row_id=album_id, kind="album",
                reason="inspect the channel before retrying",
            )
        await self.db.finish_album_failed(album_id, message)
        logger.warning("publish-failed kind=album row=%s reason=%s", album_id, message)
        return PublishOutcome(result="failed", error=message, row_id=album_id, kind="album")