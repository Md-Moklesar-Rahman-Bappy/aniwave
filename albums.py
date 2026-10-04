"""Durable album collection and finalization.

Why not PTB's ``JobQueue``?
---------------------------
``JobQueue`` needs APScheduler, which is an optional PTB extra and is **not
installed** here. More importantly, queued jobs are runtime scheduling state -
they do not survive a restart. Because SQLite is the authoritative store, album
parts are written to disk as they arrive and a small reconciler task finalizes
albums whose quiet period has elapsed. That survives restarts by construction,
and needs no extra dependency.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

from telegram import Bot
from telegram.error import TelegramError

import ui
from config import AppConfig, get_config
from caption import parse_episode
from database import Database, Status, iso
from publisher import PublishOutcome, Publisher

logger = logging.getLogger(__name__)

#: How often the reconciler looks for albums that are ready to finalize.
RECONCILE_INTERVAL_SECONDS = 1.0

#: Kinds of media that may appear in an album part.
_ALBUM_MEDIA_TYPES = ("photo", "video", "animation", "audio", "document")


@dataclass
class AlbumPart:
    """One incoming album message."""

    source_chat_id: int
    source_message_id: int
    media_group_id: str
    topic_id: int
    sender_id: int
    media_type: str
    file_id: str
    caption: Optional[str] = None


@dataclass
class FinalizeResult:
    """What happened to an album during finalization."""

    album_id: int
    action: str  # published | pending | failed | skipped
    reason: str = ""
    outcome: Optional[PublishOutcome] = None


def resolve_album_episode(items: Sequence[Dict[str, Any]]) -> Optional[str]:
    """Pick the episode number from an album's captions.

    Telegram puts an album's caption on one part (often the first or last), so
    every part caption is inspected.
    """
    for item in items:
        episode = parse_episode(item.get("original_caption"))
        if episode:
            return episode
    return None


class AlbumService:
    """Collects album parts in SQLite and finalizes them once they settle."""

    def __init__(
        self,
        database: Database,
        publisher: Publisher,
        bot: Bot,
        config: Optional[AppConfig] = None,
        *,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.db = database
        self.publisher = publisher
        self.bot = bot
        self.config = config or get_config()
        self._now = now_fn
        self._task: Optional[asyncio.Task[None]] = None
        self._stopping = asyncio.Event()

    # ------------------------------------------------------------------ #
    # Ingestion
    # ------------------------------------------------------------------ #

    async def ingest_part(self, part: AlbumPart, topic_title: str, topic_emoji: str) -> int:
        """Persist an album part idempotently and return the album id.

        Duplicate Telegram updates are absorbed by the database's uniqueness
        constraints, so replaying the same update is harmless.
        """
        received = iso(self._now())
        album_id, created = await self.db.upsert_album(
            source_chat_id=part.source_chat_id,
            media_group_id=part.media_group_id,
            topic_id=part.topic_id,
            sender_id=part.sender_id,
            anime_title=topic_title,
            emoji=topic_emoji,
            received_at=received,
        )
        added = await self.db.add_album_item(
            album_id=album_id,
            source_chat_id=part.source_chat_id,
            source_message_id=part.source_message_id,
            media_type=part.media_type,
            file_id=part.file_id,
            original_caption=part.caption,
            received_at=received,
        )
        logger.info(
            "album-part album=%s group=%s message=%s type=%s new=%s album_created=%s",
            album_id, part.media_group_id, part.source_message_id, part.media_type,
            added, created,
        )
        return album_id

    # ------------------------------------------------------------------ #
    # Finalization
    # ------------------------------------------------------------------ #

    def due_cutoff(self) -> str:
        """Timestamp before which a collecting album is considered settled."""
        return iso(self._now() - timedelta(seconds=self.config.album_quiet_seconds))

    async def finalize_due(self) -> List[FinalizeResult]:
        """Finalize every collecting album whose quiet period has elapsed."""
        albums = await self.db.albums_due(self.due_cutoff())
        results: List[FinalizeResult] = []
        for album in albums:
            results.append(await self.finalize_album(int(album["id"])))
        return results

    async def finalize_album(self, album_id: int) -> FinalizeResult:
        """Move one album out of ``collecting`` exactly once.

        The ``collecting -> pending`` transition is a compare-and-set, so two
        concurrent finalizers cannot both act on the same album.
        """
        album = await self.db.get_album(album_id)
        if album is None:
            return FinalizeResult(album_id, "skipped", "album not found")
        if album["status"] != Status.COLLECTING:
            return FinalizeResult(album_id, "skipped", f"already {album['status']}")

        await self.db.renumber_album_items(album_id)
        items = await self.db.get_album_items(album_id)
        if not items:
            return FinalizeResult(album_id, "skipped", "album has no parts")

        # Authorization and topic re-validation at publish time. A topic could
        # have been reconfigured while the album was collecting.
        if int(album["source_chat_id"]) != self.config.source_group_id:
            return FinalizeResult(album_id, "skipped", "source chat is not the configured group")
        if not self.config.is_admin(album["sender_id"]):
            return FinalizeResult(album_id, "skipped", "sender is no longer a configured admin")
        topic = self.config.topic_for(int(album["topic_id"]))
        if topic is None:
            return FinalizeResult(album_id, "skipped", "topic is not configured")

        bad_types = sorted({str(i["media_type"]) for i in items} - set(_ALBUM_MEDIA_TYPES))
        if bad_types:
            return FinalizeResult(album_id, "failed", f"unsupported media: {', '.join(bad_types)}")

        episode = resolve_album_episode(items)
        caption = ""
        if episode:
            caption = self.publisher.build_caption(
                {
                    "episode_number": episode,
                    "emoji": album["emoji"] or topic.emoji,
                    "anime_title": album["anime_title"] or topic.title,
                }
            )

        moved = await self.db.finish_album_collection(album_id, episode, caption)
        if not moved:
            # Another finalizer won the race.
            return FinalizeResult(album_id, "skipped", "another finalizer already handled it")

        logger.info(
            "album-finalized album=%s items=%d episode=%s", album_id, len(items), episode
        )

        if self.config.auto_publish:
            if not episode:
                await self._notify_needs_episode(album, topic)
                return FinalizeResult(album_id, "pending", "no episode number")
            outcome = await self.publisher.publish_album_row(album_id)
            await self._notify_outcome(album, topic, outcome)
            return FinalizeResult(album_id, outcome.result, outcome.reason, outcome)

        preview_id = await self._send_album_preview(album, topic, episode, len(items))
        if preview_id is not None:
            await self.db.set_album_preview(album_id, preview_id)

        if not episode:
            await self._notify_needs_episode(album, topic)
            return FinalizeResult(album_id, "pending", "no episode number")
        return FinalizeResult(album_id, "pending", "awaiting approval")

    # ------------------------------------------------------------------ #
    # Notifications
    # ------------------------------------------------------------------ #

    async def _send_album_preview(
        self, album: Dict[str, Any], topic, episode: Optional[str], item_count: int
    ) -> Optional[int]:
        text = ui.preview_text(
            topic_label=f"{topic.emoji} {topic.title}",
            anime_title=album["anime_title"] or topic.title,
            episode_number=episode,
            media_type="album",
            kind=ui.KIND_ALBUM,
            row_id=int(album["id"]),
            album_items=item_count,
        )
        keyboard = ui.build_preview_keyboard(ui.KIND_ALBUM, int(album["id"]))
        sent_id = await self._send_to_topic(int(album["source_chat_id"]), int(album["topic_id"]), text, keyboard)
        if sent_id is not None:
            logger.info("album-preview-sent album=%s", album["id"])
        return sent_id

    async def _notify_needs_episode(self, album: Dict[str, Any], topic) -> None:
        logger.warning(
            "album-missing-episode album=%s group=%s", album["id"], album["media_group_id"]
        )
        await self._send_to_topic(
            int(album["source_chat_id"]),
            int(album["topic_id"]),
            ui.needs_episode_text(f"{topic.emoji} {topic.title}"),
            None,
        )

    async def _notify_outcome(self, album: Dict[str, Any], topic, outcome: PublishOutcome) -> None:
        chat_id = int(album["source_chat_id"])
        thread_id = int(album["topic_id"])
        if outcome.result == "published":
            text = ui.published_text(
                topic_label=f"{topic.emoji} {topic.title}",
                anime_title=album["anime_title"] or topic.title,
                episode_number=str(album["episode_number"] or "?"),
                target_ids=outcome.target_message_ids,
            )
        elif outcome.result == "uncertain":
            text = ui.uncertain_text(ui.KIND_ALBUM, int(album["id"]), outcome.error)
        elif outcome.result == "refused":
            text = ui.failed_text(outcome.reason or outcome.error or "refused")
        else:
            text = ui.failed_text(outcome.error or outcome.reason or "unknown error")
        await self._send_to_topic(chat_id, thread_id, text, None)

    async def _send_to_topic(
        self, chat_id: int, thread_id: int, text: str, keyboard
    ) -> Optional[int]:
        """Send a message into a forum topic, never raising."""
        try:
            sent = await self.bot.send_message(
                chat_id=chat_id, text=text, message_thread_id=thread_id, reply_markup=keyboard
            )
            return int(sent.message_id)
        except TelegramError as exc:
            logger.error("notify-failed chat=%s thread=%s error=%s", chat_id, thread_id, exc)
        except Exception:  # noqa: BLE001
            logger.exception("notify-failed-unexpected chat=%s thread=%s", chat_id, thread_id)
        return None

    # ------------------------------------------------------------------ #
    # Recovery and background loop
    # ------------------------------------------------------------------ #

    async def recover_on_startup(self) -> List[FinalizeResult]:
        """Finalize albums that were mid-collection when the process stopped."""
        pending = await self.db.get_albums_by_status(Status.COLLECTING)
        logger.info("album-recovery-check collecting=%d", len(pending))
        if not pending:
            return []
        results: List[FinalizeResult] = []
        cutoff = self.due_cutoff()
        for album in pending:
            if str(album["last_item_received_at"]) <= cutoff:
                results.append(await self.finalize_album(int(album["id"])))
            else:
                logger.info(
                    "album-recovery-deferred album=%s waiting_for_quiet_period",
                    album["id"],
                )
        return results

    def start(self, application: Optional[Any] = None) -> Optional[asyncio.Task]:
        """Start the reconciler loop, tracked so shutdown can cancel it.

        ``Application.create_task`` is used only when the application is already
        running. ``post_init`` runs *before* ``Application.start()``, so at that
        point ``application.running`` is ``False`` and PTB would emit
        "tasks created while the application is not running won't be
        automatically awaited" and leave the task untracked. A plain
        :func:`asyncio.create_task` plus our own handle avoids that; the loop
        handles its own exceptions and :meth:`stop` cancels it.
        """
        if self._task is not None and not self._task.done():
            return self._task
        self._stopping.clear()
        if application is not None and getattr(application, "running", False):
            task = application.create_task(self.run_forever(), name="album-reconciler")
        else:
            task = asyncio.create_task(self.run_forever(), name="album-reconciler")
        self._task = task
        return task

    async def stop(self) -> None:
        """Stop the reconciler loop."""
        self._stopping.set()
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    async def run_forever(self, interval: float = RECONCILE_INTERVAL_SECONDS) -> None:
        """Periodically finalize settled albums."""
        logger.info("album-reconciler-started interval=%ss", interval)
        try:
            while not self._stopping.is_set():
                try:
                    results = await self.finalize_due()
                    for result in results:
                        if result.action in ("published", "failed"):
                            logger.info(
                                "album-reconciler-action album=%s action=%s",
                                result.album_id, result.action,
                            )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception("album-reconciler-iteration-failed")
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            logger.info("album-reconciler-stopped")
            raise