"""Telegram update handlers: commands, media intake, callbacks, and the
Edit Episode conversation.

Authorization is always by numeric id from ``ADMIN_IDS`` - never by username -
and every privileged entry point tolerates a missing user, chat, message or
callback query without raising.
"""

from __future__ import annotations

import logging
import time
import warnings
from typing import Any, Dict, List, Optional, Tuple

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

import ui
from albums import AlbumPart, AlbumService
from caption import EpisodeValidationError, parse_episode, validate_episode_input
from config import AppConfig, get_config
from database import EDITABLE_STATES, SUPPORTED_MEDIA_TYPES, Database, Status
from publisher import Publisher

logger = logging.getLogger(__name__)

#: Conversation state while an admin supplies a new episode number.
STATE_EDIT_EPISODE = 0

#: How long an Edit Episode session stays open.
EDIT_CONVERSATION_TIMEOUT = 300

_UNAUTHORIZED = "You are not authorized to use this bot."

#: Attributes that make a message "supported media" for our purposes.
_MEDIA_ATTRIBUTES: Tuple[str, ...] = ("video", "animation", "audio", "photo", "document")


class SupportedMediaFilter(filters.MessageFilter):
    """Accept only the media types this bot publishes.

    ``filters.Document`` in python-telegram-bot 21 is not a ``BaseFilter``
    subclass and cannot be composed with ``|``, so a dedicated ``MessageFilter``
    is used instead of a merged expression.

    Album parts are accepted too: every part carries a supported media type and
    a ``media_group_id``.
    """

    def filter(self, message) -> bool:  # noqa: D102 - see class docstring
        if message is None:
            return False
        if getattr(message, "media_group_id", None):
            return True
        return any(getattr(message, attribute, None) is not None for attribute in _MEDIA_ATTRIBUTES)


SUPPORTED_MEDIA = SupportedMediaFilter()


def describe_media_type(message) -> Optional[str]:
    for attribute in _MEDIA_ATTRIBUTES:
        if getattr(message, attribute, None) is not None:
            return attribute
    return None


def extract_file_id(message, media_type: str) -> Optional[str]:
    """Pick the right file id (largest photo size for photos)."""
    if media_type == "photo":
        sizes = getattr(message, "photo", None) or []
        return getattr(sizes[-1], "file_id", None) if sizes else None
    media = getattr(message, media_type, None)
    return getattr(media, "file_id", None)


def extract_file_unique_id(message, media_type: str) -> str:
    if media_type == "photo":
        sizes = getattr(message, "photo", None) or []
        return getattr(sizes[-1], "file_unique_id", "") if sizes else ""
    media = getattr(message, media_type, None)
    return getattr(media, "file_unique_id", "") or ""


class Handlers:
    """All update handlers for the bot."""

    def __init__(
        self,
        database: Database,
        publisher: Publisher,
        albums: AlbumService,
        config: Optional[AppConfig] = None,
    ) -> None:
        self.db = database
        self.publisher = publisher
        self.albums = albums
        self.config = config or get_config()
        self.bot = publisher.bot

    # ------------------------------------------------------------------ #
    # Authorization / routing helpers
    # ------------------------------------------------------------------ #

    def _admin_id(self, update: Update) -> Optional[int]:
        user = update.effective_user
        return getattr(user, "id", None) if user is not None else None

    def _is_admin_update(self, update: Update) -> bool:
        return self.config.is_admin(self._admin_id(update))

    def _reject_unauthorized(self, update: Update) -> None:
        """Log a rejected authorization attempt. Safe to call with a partial update."""
        chat_id = None
        chat = getattr(update, "effective_chat", None)
        if chat is not None:
            chat_id = getattr(chat, "id", None)
        logger.warning(
            "auth-rejected user=%s chat=%s", self._admin_id(update), chat_id
        )

    async def _reply(self, update: Update, text: str, **kwargs) -> None:
        message = update.effective_message
        if message is None:
            return
        try:
            await message.reply_text(text, **kwargs)
        except TelegramError:
            logger.exception("reply-failed")

    async def _answer(self, query, text: str, *, alert: bool = False) -> None:
        if query is None:
            return
        try:
            await query.answer(text, show_alert=alert)
        except TelegramError:
            logger.exception("callback-answer-failed")

    async def _edit_query_message(self, query, text: str, keyboard=None) -> None:
        if query is None or query.message is None:
            return
        try:
            await query.edit_message_text(text, reply_markup=keyboard)
        except TelegramError:
            # Editing an unchanged message is not an error worth surfacing.
            logger.debug("callback-edit-message-failed", exc_info=True)

    def _thread_id(self, update: Update) -> Optional[int]:
        message = update.effective_message
        return getattr(message, "message_thread_id", None) if message else None

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin_update(update):
            self._reject_unauthorized(update)
            await self._reply(update, _UNAUTHORIZED)
            return
        await self._reply(update, ui.help_text(config=self.config))

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin_update(update):
            await self._reply(update, _UNAUTHORIZED)
            return
        await self._reply(update, ui.help_text(config=self.config))

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin_update(update):
            await self._reply(update, _UNAUTHORIZED)
            return
        database_ok = await self.db.health_check()
        counts_items = await self.db.counts_by_state("media_items")
        counts_albums = await self.db.counts_by_state("albums")
        await self._reply(
            update,
            ui.status_text(
                config=self.config,
                counts_items=counts_items,
                counts_albums=counts_albums,
                database_ok=database_ok,
            ),
        )

    async def cmd_debug(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin_update(update):
            await self._reply(update, _UNAUTHORIZED)
            return
        await self._reply(update, ui.debug_text(update=update, config=self.config))

    async def cmd_chatid(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin_update(update):
            await self._reply(update, _UNAUTHORIZED)
            return
        await self._reply(
            update, ui.chat_id_text(chat=update.effective_chat, user=update.effective_user)
        )

    async def cmd_topicid(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin_update(update):
            await self._reply(update, _UNAUTHORIZED)
            return
        await self._reply(
            update, ui.topic_id_text(message=update.effective_message, config=self.config)
        )

    async def cmd_uncertain(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """List interrupted publications that need a manual decision."""
        if not self._is_admin_update(update):
            await self._reply(update, _UNAUTHORIZED)
            return
        rows = await self.db.uncertain_rows()
        if not rows:
            await self._reply(update, "No interrupted publications need review.")
            return

        lines = ["Interrupted publications needing review:", ""]
        for row in rows:
            kind = row.get("kind", "item")
            row_id = int(row["id"])
            title = row.get("anime_title") or "unknown"
            episode = row.get("episode_number") or "?"
            error = row.get("last_error") or "unknown reason"
            lines.append(f"{kind}:{row_id} - {title} episode {episode}")
            lines.append(f"  reason: {error}")
            if kind == ui.KIND_ALBUM:
                lines.append(f"  parts: {row.get('item_count', '?')}")
            lines.append("")
        lines.append("Nothing here is retried automatically. Use /retry only after resolving.")
        await self._reply(update, "\n".join(lines))

    async def cmd_retry(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Retry a failed record. Refuses published/uncertain/canceled rows."""
        if not self._is_admin_update(update):
            await self._reply(update, _UNAUTHORIZED)
            return

        args = list(context.args or [])
        if not args:
            await self._reply(update, "Usage: /retry <id>  or  /retry m:<id>  or  /retry a:<id>")
            return

        target = ui.parse_target(args[0])
        if target is None:
            await self._reply(update, f"Could not read {args[0]!r}. Use an id such as 12, m:12 or a:12.")
            return
        kind, row_id = target

        if kind == ui.KIND_ALBUM:
            row = await self.db.get_album(row_id)
        else:
            row = await self.db.get_item(row_id)
        if row is None:
            await self._reply(update, f"No {kind}:{row_id} record found.")
            return

        status = str(row["status"])
        if status == Status.FAILED:
            pass
        elif status == Status.UNCERTAIN:
            await self._reply(
                update,
                f"{kind}:{row_id} is uncertain - the bot stopped mid-publish and the post may "
                f"already exist. Check the channel, then use /uncertain to resolve it.",
            )
            return
        elif status == Status.PUBLISHED:
            await self._reply(update, f"{kind}:{row_id} is already published.")
            return
        elif status == Status.CANCELED:
            await self._reply(update, f"{kind}:{row_id} was canceled and cannot be retried.")
            return
        elif status == Status.PUBLISHING:
            await self._reply(update, f"{kind}:{row_id} is already publishing.")
            return
        else:
            await self._reply(
                update,
                f"{kind}:{row_id} is {status}. Only failed records can be retried; "
                f"press Publish on its preview instead.",
            )
            return

        if kind == ui.KIND_ALBUM:
            outcome = await self.publisher.publish_album_row(row_id)
        else:
            outcome = await self.publisher.publish_item_row(row_id)

        await self._reply(update, self._outcome_text(outcome, row))

    def _outcome_text(self, outcome, row: Dict[str, Any]) -> str:
        topic = self.config.topic_for(int(row["topic_id"])) if row else None
        label = f"{topic.emoji} {topic.title}" if topic else "unknown topic"
        if outcome.result == "published":
            return ui.published_text(
                topic_label=label,
                anime_title=row.get("anime_title") or "unknown",
                episode_number=str(row.get("episode_number") or "?"),
                target_ids=outcome.target_message_ids,
            )
        if outcome.result == "uncertain":
            return ui.uncertain_text(outcome.kind, int(outcome.row_id or 0), outcome.error)
        if outcome.result == "refused":
            return f"Not published: {outcome.reason or outcome.error}"
        return ui.failed_text(outcome.error or outcome.reason or "unknown error")

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin_update(update):
            await self._reply(update, _UNAUTHORIZED)
            return
        await self._reply(update, "Cancelled.")

    # ------------------------------------------------------------------ #
    # Media intake
    # ------------------------------------------------------------------ #

    def _media_route(self, update: Update) -> Optional[Tuple[int, Any]]:
        """Validate a media message and return ``(topic_id, TopicConfig)``."""
        message = update.effective_message
        if message is None:
            return None
        user = update.effective_user
        if user is None or getattr(user, "is_bot", False):
            return None
        if getattr(message, "edit_date", None):
            return None
        chat = update.effective_chat
        if chat is None or getattr(chat, "type", None) != "supergroup":
            return None
        if int(getattr(chat, "id", 0)) != self.config.source_group_id:
            return None
        if not self.config.is_admin(getattr(user, "id", None)):
            return None

        topic_id = getattr(message, "message_thread_id", None)
        if topic_id is None:
            return None
        topic = self.config.topic_for(int(topic_id))
        if topic is None:
            return None
        return int(topic_id), topic

    async def handle_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Route a supported-media message to the album collector or the
        single-upload pipeline."""
        route = self._media_route(update)
        if route is None:
            logger.debug(
                "media-ignored user=%s chat=%s",
                self._admin_id(update),
                getattr(update.effective_chat, "id", None),
            )
            return
        topic_id, topic = route
        message = update.effective_message
        assert message is not None
        user = update.effective_user
        assert user is not None
        chat = update.effective_chat
        assert chat is not None

        media_type = describe_media_type(message)
        if media_type is None or media_type not in SUPPORTED_MEDIA_TYPES:
            logger.debug("media-unsupported-type type=%r", media_type)
            return
        file_id = extract_file_id(message, media_type)
        if not file_id:
            logger.debug("media-missing-file-id type=%s", media_type)
            return

        if getattr(message, "media_group_id", None):
            part = AlbumPart(
                source_chat_id=int(chat.id),
                source_message_id=int(message.message_id),
                media_group_id=str(message.media_group_id),
                topic_id=topic_id,
                sender_id=int(user.id),
                media_type=media_type,
                file_id=file_id,
                caption=message.caption,
            )
            await self.albums.ingest_part(part, topic.title, topic.emoji)
            return

        await self._handle_single_media(
            update=update,
            source_chat_id=int(chat.id),
            source_message_id=int(message.message_id),
            topic_id=topic_id,
            sender_id=int(user.id),
            media_type=media_type,
            file_id=file_id,
            file_unique_id=extract_file_unique_id(message, media_type),
            caption=message.caption,
            topic=topic,
        )

    async def _handle_single_media(
        self,
        *,
        update: Update,
        source_chat_id: int,
        source_message_id: int,
        topic_id: int,
        sender_id: int,
        media_type: str,
        file_id: str,
        file_unique_id: str,
        caption: Optional[str],
        topic,
    ) -> None:
        episode = parse_episode(caption)
        inserted, row_id = await self.db.insert_media_item(
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            topic_id=topic_id,
            sender_id=sender_id,
            media_type=media_type,
            file_id=file_id,
            file_unique_id=file_unique_id,
            caption=caption,
            anime_title=topic.title,
            emoji=topic.emoji,
            episode_number=episode,
        )
        if not inserted:
            # Duplicate Telegram update: the database already owns this message.
            logger.info(
                "media-duplicate chat=%s message=%s", source_chat_id, source_message_id
            )
            return

        logger.info(
            "media-accepted chat=%s message=%s type=%s episode=%s topic=%s",
            source_chat_id, source_message_id, media_type, episode, topic_id,
        )

        if self.config.auto_publish:
            if not episode:
                await self._reply(update, ui.needs_episode_text(f"{topic.emoji} {topic.title}"))
                return
            outcome = await self.publisher.publish_item_row(row_id)
            row = await self.db.get_item(row_id) or {}
            await self._reply(update, self._outcome_text(outcome, row))
            return

        text = ui.preview_text(
            topic_label=f"{topic.emoji} {topic.title}",
            anime_title=topic.title,
            episode_number=episode,
            media_type=media_type,
            kind=ui.KIND_ITEM,
            row_id=row_id,
        )
        if not episode:
            text = f"{text}\n\n{ui.no_episode_hint()}"

        keyboard = ui.build_preview_keyboard(ui.KIND_ITEM, row_id)
        message = update.effective_message
        if message is None:
            return
        try:
            sent = await message.reply_text(text, reply_markup=keyboard)
        except TelegramError:
            logger.exception("preview-send-failed row=%s", row_id)
            return
        await self.db.set_item_preview(row_id, int(sent.message_id))

    # ------------------------------------------------------------------ #
    # Callback queries
    # ------------------------------------------------------------------ #

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        if not self.config.is_admin(getattr(query.from_user, "id", None)):
            await self._answer(query, "Not authorized", alert=True)
            return

        decoded = ui.decode_callback(getattr(query, "data", None))
        if decoded is None:
            await self._answer(query, "Unknown action", alert=True)
            return
        action, kind, row_id = decoded

        logger.info(
            "callback kind=%s row=%s action=%s user=%s",
            kind, row_id, action, getattr(query.from_user, "id", None),
        )

        if action == ui.ACT_PUBLISH:
            await self._cb_publish(query, kind, row_id)
        elif action == ui.ACT_CANCEL:
            await self._cb_cancel(query, kind, row_id)
        elif action == ui.ACT_UNCERTAIN_PUBLISHED:
            await self._cb_uncertain_published(query, kind, row_id)
        elif action == ui.ACT_UNCERTAIN_ABSENT:
            await self._cb_uncertain_absent(query, kind, row_id)
        elif action == ui.ACT_CONFIRM_ABSENT:
            await self._cb_confirm_absent(query, kind, row_id)
        else:
            await self._answer(query, "Unknown action", alert=True)

    async def _load_row(self, kind: str, row_id: int) -> Optional[Dict[str, Any]]:
        if kind == ui.KIND_ALBUM:
            return await self.db.get_album(row_id)
        return await self.db.get_item(row_id)

    async def _cb_publish(self, query, kind: str, row_id: int) -> None:
        row = await self._load_row(kind, row_id)
        if row is None:
            await self._answer(query, "Record not found", alert=True)
            return
        status = str(row["status"])
        if status in (Status.PUBLISHED, Status.CANCELED, Status.UNCERTAIN, Status.PUBLISHING):
            await self._answer(query, f"Cannot publish from state {status}", alert=True)
            return
        if status not in EDITABLE_STATES:
            await self._answer(query, f"Cannot publish from state {status}", alert=True)
            return

        outcome = (
            await self.publisher.publish_album_row(row_id)
            if kind == ui.KIND_ALBUM
            else await self.publisher.publish_item_row(row_id)
        )
        if outcome.result == "published":
            await self._answer(query, "Published")
            await self._edit_query_message(query, self._outcome_text(outcome, row))
        elif outcome.result == "uncertain":
            await self._answer(query, "Outcome unknown - manual check required", alert=True)
            await self._edit_query_message(
                query,
                ui.uncertain_text(kind, row_id, outcome.error),
                ui.build_uncertain_keyboard(kind, row_id),
            )
        else:
            await self._answer(query, "Publishing failed", alert=True)
            await self._edit_query_message(
                query, ui.failed_text(outcome.error or outcome.reason or "unknown error")
            )

    async def _cb_cancel(self, query, kind: str, row_id: int) -> None:
        if kind == ui.KIND_ALBUM:
            changed = await self.db.cancel_album(row_id)
        else:
            changed = await self.db.cancel_item(row_id)
        if not changed:
            row = await self._load_row(kind, row_id)
            state = row["status"] if row else "missing"
            await self._answer(query, f"Cannot cancel from state {state}", alert=True)
            return
        logger.info("record-canceled kind=%s row=%s", kind, row_id)
        await self._answer(query, "Canceled")
        await self._edit_query_message(query, f"Canceled {kind}:{row_id}.")

    async def _cb_uncertain_published(self, query, kind: str, row_id: int) -> None:
        row = await self._load_row(kind, row_id)
        if row is None or row["status"] != Status.UNCERTAIN:
            await self._answer(query, "This record is no longer uncertain", alert=True)
            return
        if kind == ui.KIND_ALBUM:
            ok = await self.db.resolve_album_uncertain_published(row_id)
        else:
            ok = await self.db.resolve_item_uncertain_published(row_id)
        if not ok:
            await self._answer(query, "State changed - nothing to do", alert=True)
            return
        logger.info("uncertain-resolved-published kind=%s row=%s", kind, row_id)
        await self._answer(query, "Marked as published")
        await self._edit_query_message(query, f"{kind}:{row_id} confirmed published.")

    async def _cb_uncertain_absent(self, query, kind: str, row_id: int) -> None:
        row = await self._load_row(kind, row_id)
        if row is None or row["status"] != Status.UNCERTAIN:
            await self._answer(query, "This record is no longer uncertain", alert=True)
            return
        await self._answer(query, "Confirm carefully")
        await self._edit_query_message(
            query,
            "Are you sure it is NOT published?\n\n"
            "Confirming allows the bot to publish it again, which would create a duplicate "
            "if you were wrong.",
            ui.build_confirm_absent_keyboard(kind, row_id),
        )

    async def _cb_confirm_absent(self, query, kind: str, row_id: int) -> None:
        row = await self._load_row(kind, row_id)
        if row is None or row["status"] != Status.UNCERTAIN:
            await self._answer(query, "This record is no longer uncertain", alert=True)
            return
        if kind == ui.KIND_ALBUM:
            ok = await self.db.resolve_album_uncertain_absent(row_id)
            new_state = Status.PENDING
        else:
            ok = await self.db.resolve_item_uncertain_absent(row_id)
            new_state = Status.FAILED
        if not ok:
            await self._answer(query, "State changed - nothing to do", alert=True)
            return
        logger.info("uncertain-resolved-absent kind=%s row=%s state=%s", kind, row_id, new_state)
        await self._answer(query, "Resolved")
        await self._edit_query_message(
            query,
            f"{kind}:{row_id} confirmed NOT published.\nState is now {new_state}; use /retry to publish.",
        )

    # ------------------------------------------------------------------ #
    # Edit Episode conversation
    # ------------------------------------------------------------------ #

    async def cb_edit_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Entry point for the Edit Episode conversation."""
        query = update.callback_query
        if query is None:
            return ConversationHandler.END
        if not self.config.is_admin(getattr(query.from_user, "id", None)):
            await self._answer(query, "Not authorized", alert=True)
            return ConversationHandler.END

        decoded = ui.decode_callback(getattr(query, "data", None))
        if decoded is None:
            await self._answer(query, "Unknown action", alert=True)
            return ConversationHandler.END
        _, kind, row_id = decoded

        row = await self._load_row(kind, row_id)
        if row is None:
            await self._answer(query, "Record not found", alert=True)
            return ConversationHandler.END
        status = str(row["status"])
        if status not in EDITABLE_STATES:
            await self._answer(query, f"Cannot edit from state {status}", alert=True)
            return ConversationHandler.END

        context.user_data["edit_kind"] = kind
        context.user_data["edit_row_id"] = row_id
        context.user_data["edit_admin_id"] = getattr(query.from_user, "id", None)
        context.user_data["edit_started_at"] = time.monotonic()

        current = row.get("episode_number") or "not set"
        await self._answer(query, "Send the new episode number")
        await self._edit_query_message(
            query,
            "Edit episode number\n\n"
            f"Current episode: {current}\n"
            "Send a positive number, for example 1165 or 12.5.\n"
            "Use /cancel to abort.",
        )
        return STATE_EDIT_EPISODE

    async def on_episode_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Validate and persist a new episode number."""
        kind = context.user_data.get("edit_kind")
        row_id = context.user_data.get("edit_row_id")
        owner = context.user_data.get("edit_admin_id")
        message = update.effective_message

        if kind is None or row_id is None or message is None:
            return ConversationHandler.END

        if not self.config.is_admin(self._admin_id(update)):
            await self._reply(update, _UNAUTHORIZED)
            return ConversationHandler.END

        # PTB's own conversation_timeout is inert without the job-queue extra,
        # so the session deadline is enforced here.
        started = context.user_data.get("edit_started_at")
        if isinstance(started, (int, float)) and (time.monotonic() - started) > EDIT_CONVERSATION_TIMEOUT:
            logger.info(
                "edit-conversation-expired kind=%s row=%s user=%s", kind, row_id, self._admin_id(update)
            )
            self._clear_edit_state(context)
            await self._reply(update, "This edit session expired. Press Edit Episode to start again.")
            return ConversationHandler.END

        if owner is not None and self._admin_id(update) != owner:
            await self._reply(update, "Another admin started this edit session.")
            return STATE_EDIT_EPISODE

        text = getattr(message, "text", None)
        try:
            episode = validate_episode_input(text)
        except EpisodeValidationError as exc:
            logger.info("edit-episode-rejected admin=%s reason=%s", self._admin_id(update), exc)
            await self._reply(update, f"{exc}\nSend it again, or use /cancel.")
            return STATE_EDIT_EPISODE

        # Re-read state immediately before applying the change.
        row = await self._load_row(kind, row_id)
        if row is None:
            await self._reply(update, "Record not found.")
            return ConversationHandler.END
        status = str(row["status"])
        if status not in EDITABLE_STATES:
            await self._reply(update, f"This record is {status} and can no longer be edited.")
            return ConversationHandler.END

        caption = self.publisher.build_caption(
            {
                "episode_number": episode,
                "emoji": row.get("emoji") or "",
                "anime_title": row.get("anime_title") or "",
            }
        )
        if kind == ui.KIND_ALBUM:
            ok = await self.db.set_album_episode(row_id, episode, caption)
        else:
            ok = await self.db.set_item_episode(row_id, episode, caption)
        if not ok:
            logger.info(
                "edit-episode-state-changed kind=%s row=%s state=%s", kind, row_id, status
            )
            await self._reply(update, f"Could not update episode: state changed to {status}.")
            return ConversationHandler.END

        logger.info("edit-episode-applied kind=%s row=%s episode=%s", kind, row_id, episode)
        await self._refresh_preview(kind, row_id, row, episode)
        await self._reply(update, f"Episode set to {episode}.")

        self._clear_edit_state(context)
        return ConversationHandler.END

    @staticmethod
    def _clear_edit_state(context: ContextTypes.DEFAULT_TYPE) -> None:
        for key in ("edit_kind", "edit_row_id", "edit_admin_id", "edit_started_at"):
            context.user_data.pop(key, None)

    async def _refresh_preview(self, kind: str, row_id: int, row: Dict[str, Any], episode: str) -> None:
        """Update the stored preview message so buttons reflect the new value."""
        preview_id = row.get("preview_message_id")
        if not preview_id:
            return
        topic = self.config.topic_for(int(row["topic_id"]))
        label = f"{topic.emoji} {topic.title}" if topic else "unknown topic"
        text = ui.preview_text(
            topic_label=label,
            anime_title=row.get("anime_title") or "unknown",
            episode_number=episode,
            media_type=row.get("media_type") or "album",
            kind=kind,
            row_id=row_id,
            album_items=row.get("item_count") if kind == ui.KIND_ALBUM else None,
        )
        keyboard = ui.build_preview_keyboard(kind, row_id)
        try:
            await self.bot.edit_message_text(
                chat_id=int(row["source_chat_id"]),
                message_id=int(preview_id),
                text=text,
                reply_markup=keyboard,
            )
        except TelegramError:
            logger.debug("preview-refresh-failed row=%s", row_id, exc_info=True)

    async def on_conversation_timeout(self, update: Update) -> None:
        logger.info("edit-conversation-timeout user=%s", self._admin_id(update))

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #

    def build_edit_conversation(self) -> ConversationHandler:
        """Build the Edit Episode conversation.

        Two python-telegram-bot specifics are handled deliberately:

        * ``per_message=False`` is required. With ``per_message=True`` the
          conversation is keyed by the callback's message id, so the follow-up
          text message (a *different* message) is rejected and the workflow can
          never complete. python-telegram-bot emits a ``PTBUserWarning`` about
          this; the warning is suppressed only for this construction, with
          justification, because the alternative is a broken feature.
        * ``conversation_timeout`` is silently ignored unless the optional
          ``job-queue`` extra (APScheduler) is installed, which it is not here.
          The timeout is therefore enforced by :meth:`on_episode_input` against
          ``edit_started_at``.
        """
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r".*per_message=False.*",
                category=PTBUserWarning,
            )
            return ConversationHandler(
                entry_points=[
                    CallbackQueryHandler(self.cb_edit_entry, pattern=r"^aw:e:[ma]:[0-9]{1,12}$"),
                ],
                states={
                    STATE_EDIT_EPISODE: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_episode_input),
                    ],
                },
                fallbacks=[CommandHandler("cancel", self.cmd_cancel)],
                conversation_timeout=EDIT_CONVERSATION_TIMEOUT,
                per_user=True,
                per_chat=False,
                per_message=False,
                allow_reentry=False,
                name="edit_episode",
            )

    def get_handlers(self) -> List[Any]:
        """Handler registration order matters.

        The conversation is registered first: python-telegram-bot evaluates
        handlers in order and ``ConversationHandler`` blocks, so it must see the
        ``aw:e:`` callback before the generic callback handler does.
        """
        return [
            self.build_edit_conversation(),
            CommandHandler("start", self.cmd_start),
            CommandHandler("help", self.cmd_help),
            CommandHandler("status", self.cmd_status),
            CommandHandler("debug", self.cmd_debug),
            CommandHandler("chatid", self.cmd_chatid),
            CommandHandler("topicid", self.cmd_topicid),
            CommandHandler("retry", self.cmd_retry),
            CommandHandler("uncertain", self.cmd_uncertain),
            MessageHandler(SUPPORTED_MEDIA, self.handle_media, block=False),
            CallbackQueryHandler(self.handle_callback, pattern=r"^aw:(?!e:)"),
        ]