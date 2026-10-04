import asyncio
import logging
from typing import Optional

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, Message
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, CallbackQueryHandler, MessageHandler, CommandHandler, filters as ft

from config import get_config, TopicConfig, get_topic_config
from database import Database
from caption import parse_episode
from publisher import Publisher

logger = logging.getLogger(__name__)


class Handlers:
    def __init__(self, database: Database, publisher: Publisher) -> None:
        self.db = database
        self.publisher = publisher
        self.config = get_config()
        self._album_timers: dict = {}

    def _is_admin(self, user_id: int) -> bool:
        return user_id in self.config.admin_ids

    def _is_bot_message(self, message: Message) -> bool:
        return message.from_user and message.from_user.is_bot

    def _is_service_message(self, message: Message) -> bool:
        if not message.from_user:
            return True
        return False

    def _get_topic_id(self, update: Update) -> Optional[int]:
        if update.message and update.message.message_thread_id:
            return update.message.message_thread_id
        return None

    def _is_configured_topic(self, topic_id: Optional[int]) -> bool:
        if topic_id is None:
            return False
        from config import TOPIC_MAP
        return topic_id in TOPIC_MAP

    def _should_process(self, update: Update) -> bool:
        message = update.message
        if not message:
            return False
        if self._is_bot_message(message):
            return False
        if message.edit_date:
            return False
        if self._is_service_message(message):
            return False
        if message.chat.type != "supergroup":
            return False
        if message.chat.id != self.config.source_group_id:
            return False
        if not self._is_admin(message.from_user.id):
            return False
        topic_id = self._get_topic_id(update)
        if topic_id is None or not self._is_configured_topic(topic_id):
            return False
        return True

    def _has_media(self, message: Message) -> bool:
        if not message:
            return False
        return (
            message.video is not None
            or message.document is not None
            or message.animation is not None
            or message.audio is not None
            or message.photo is not None
            or message.media_group_id is not None
        )

    def _get_media_info(self, message: Message) -> Optional[dict]:
        if message.video:
            return {
                "media_type": "video",
                "file_id": message.video.file_id,
                "file_unique_id": message.video.file_unique_id,
                "caption": message.caption,
            }
        if message.document:
            return {
                "media_type": "document",
                "file_id": message.document.file_id,
                "file_unique_id": message.document.file_unique_id,
                "caption": message.caption,
            }
        if message.animation:
            return {
                "media_type": "animation",
                "file_id": message.animation.file_id,
                "file_unique_id": message.animation.file_unique_id,
                "caption": message.caption,
            }
        if message.audio:
            return {
                "media_type": "audio",
                "file_id": message.audio.file_id,
                "file_unique_id": message.audio.file_unique_id,
                "caption": message.caption,
            }
        if message.photo:
            return {
                "media_type": "photo",
                "file_id": message.photo[-1].file_id,
                "file_unique_id": message.photo[-1].file_unique_id,
                "caption": message.caption,
            }
        return None

    def _extract_anime_info(self, topic_id: int) -> tuple:
        topic = get_topic_config(topic_id)
        if topic:
            return topic.title, topic.emoji
        return "", ""

    async def handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update.effective_user.id):
            return
        text = (
            "🤖 **AniWave Bot** is running.\n\n"
            f"📌 Source Group: `{self.config.source_group_id}`\n"
            f"📢 Target Channel: {self.config.target_channel}\n"
            f"⚡ Mode: {'Auto-Publish' if self.config.auto_publish else 'Safe Approval'}\n\n"
            "Use /help for usage instructions."
        )
        await update.message.reply_text(text)

    async def handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update.effective_user.id):
            return
        text = (
            "📖 **How to use:**\n\n"
            "1️⃣ Upload supported media (video, photo, document, audio, animation) "
            "to any configured forum topic.\n"
            "2️⃣ Add a caption with the episode number (e.g., `EP 1165`, `Episode 25`, `1165`).\n"
            "3️⃣ Bot saves item as pending.\n"
            "4️⃣ Admin presses **Publish** to go live.\n\n"
            "Commands: /start, /help, /status, /chatid, /topicid, /debug, /retry\n"
        )
        await update.message.reply_text(text)

    async def handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update.effective_user.id):
            return
        pending = await self.db.count_pending()
        db_ok = self.db._connection is not None
        topic_ids = await self.db.get_all_configured_topic_ids()
        text = (
            "📊 **Bot Status**\n\n"
            f"🟢 Running: Yes\n"
            f"📌 Source Group: `{self.config.source_group_id}`\n"
            f"📢 Target Channel: {self.config.target_channel}\n"
            f"⚡ Auto-Publish: {'Yes' if self.config.auto_publish else 'No'}\n"
            f"💾 Database: {'OK' if db_ok else 'ERROR'}\n"
            f"📚 Topics: {', '.join(str(t) for t in topic_ids)}\n"
            f"⏳ Pending Items: {pending}"
        )
        await update.message.reply_text(text)

    async def handle_chatid(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update.effective_user.id):
            return
        msg = update.message
        text = (
            f"🆔 **Chat Info**\n\n"
            f"Chat ID: `{msg.chat.id}`\n"
            f"Title: {msg.chat.title}\n"
            f"Type: {msg.chat.type}\n"
            f"Sender ID: {msg.from_user.id}"
        )
        await msg.reply_text(text)

    async def handle_topicid(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update.effective_user.id):
            return
        msg = update.message
        topic_id = msg.message_thread_id
        from config import TOPIC_MAP
        topic_cfg = TOPIC_MAP.get(topic_id)
        topic_info = f"{topic_cfg.emoji} {topic_cfg.title}" if topic_cfg else "Not configured"
        text = (
            f"🆔 **Topic Info**\n\n"
            f"Topic ID: `{topic_id}`\n"
            f"Detected: {topic_info}"
        )
        await msg.reply_text(text)

    async def handle_debug(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update.effective_user.id):
            return
        msg = update.message
        from config import TOPIC_MAP
        topic_id = msg.message_thread_id
        topic_cfg = TOPIC_MAP.get(topic_id)
        text = (
            f"🔍 **Debug Info**\n\n"
            f"Chat ID: `{msg.chat.id}`\n"
            f"Topic ID: `{topic_id}`\n"
            f"Chat Title: {msg.chat.title}\n"
            f"Sender ID: {msg.from_user.id}\n"
            f"Topic Config: {topic_cfg.title if topic_cfg else 'None'}"
        )
        await msg.reply_text(text)

    async def handle_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if not self._should_process(update):
            return
        if not self._has_media(message):
            return

        topic_id = self._get_topic_id(update)
        if topic_id is None:
            return
        topic_cfg = get_topic_config(topic_id)
        if not topic_cfg:
            return

        anime_title = topic_cfg.title
        emoji = topic_cfg.emoji
        sender_id = message.from_user.id

        if message.media_group_id:
            await self._handle_album(update, context, message, topic_id, topic_cfg)
        else:
            await self._handle_single(update, message, topic_id, topic_cfg, anime_title, emoji, sender_id)

    async def _handle_single(
        self, update: Update, message: Message, topic_id: int,
        topic_cfg: TopicConfig, anime_title: str, emoji: str, sender_id: int
    ) -> None:
        info = self._get_media_info(message)
        if not info:
            return

        episode = parse_episode(info.get("caption"))
        source_message_id = message.message_id
        source_chat_id = message.chat.id

        item = {
            "source_chat_id": source_chat_id,
            "source_message_id": source_message_id,
            "media_group_id": None,
            "topic_id": topic_id,
            "sender_id": sender_id,
            "media_type": info["media_type"],
            "file_id": info["file_id"],
            "file_unique_id": info["file_unique_id"],
            "caption": info.get("caption"),
            "anime_title": anime_title,
            "emoji": emoji,
            "episode_number": episode or "",
            "status": "pending",
            "final_caption": "",
        }

        inserted, item_id = await self.db.insert_or_ignore(item)
        if not inserted:
            existing = await self.db.get_by_source(source_chat_id, source_message_id)
            if existing and existing.get("status") == "published":
                return
            if existing:
                item_id = existing["id"]
                await self.db.update_status(item_id, "pending")
            else:
                return

        if episode is None:
            warning = (
                "⚠️ **Cannot publish automatically.**\n\n"
                "Episode number could not be extracted from caption.\n"
                "Supported formats: `EP 1165`, `Episode 1165`, `1165`, `12.5`\n\n"
                "Use **Edit Episode** button to set manually, or edit caption and use `/retry`."
            )
            await self.publisher.send_admin_warning(update, warning)
            return

        if self.config.auto_publish:
            await self._auto_publish_item(update, item_id, source_chat_id, source_message_id, info["media_type"], info["file_id"], info.get("caption"), topic_cfg, episode)
        else:
            await self.publisher.send_preview(
                update=update,
                item_id=item_id,
                emoji=emoji,
                anime_title=anime_title,
                episode_number=episode,
                media_type=info["media_type"],
            )

    async def _handle_album(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        message: Message, topic_id: int, topic_cfg: TopicConfig
    ) -> None:
        media_group_id = message.media_group_id
        source_chat_id = message.chat.id
        source_message_id = message.message_id

        existing = await self.db.get_by_media_group(media_group_id, source_chat_id)
        if existing and existing.get("status") == "published":
            return

        if media_group_id not in self._album_timers:
            self._album_timers[media_group_id] = {"messages": [], "task": None}

        album = self._album_timers[media_group_id]
        album["messages"].append({
            "message_id": source_message_id,
            "media_type": self._get_media_type(message),
            "file_id": self._get_file_id(message),
            "caption": message.caption,
        })

        if album["task"] is None or album["task"].done():
            async def process_album():
                await asyncio.sleep(2.0)
                self._album_timers.pop(media_group_id, None)
                await self._collect_and_publish_album(context, media_group_id, source_chat_id, topic_id, topic_cfg)

            task = asyncio.create_task(process_album())
            album["task"] = task

    def _get_media_type(self, message: Message) -> str:
        if message.video: return "video"
        if message.document: return "document"
        if message.animation: return "animation"
        if message.audio: return "audio"
        if message.photo: return "photo"
        return "unknown"

    def _get_file_id(self, message: Message) -> str:
        if message.photo: return message.photo[-1].file_id
        if message.video: return message.video.file_id
        if message.document: return message.document.file_id
        if message.animation: return message.animation.file_id
        if message.audio: return message.audio.file_id
        return ""

    async def _collect_and_publish_album(
        self, context: ContextTypes.DEFAULT_TYPE, media_group_id: str,
        source_chat_id: int, topic_id: int,
        topic_cfg: TopicConfig
    ) -> None:
        pass

    async def _auto_publish_item(
        self, update: Update, item_id: int, source_chat_id: int,
        source_message_id: int, media_type: str, file_id: str,
        caption: Optional[str], topic_cfg: TopicConfig, episode: str
    ) -> None:
        try:
            target_msg_ids = await self.publisher.publish_item(
                item_id=item_id,
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
                media_type=media_type,
                file_id=file_id,
                caption=caption,
                target_chat_id=self.config.target_channel,
                emoji=topic_cfg.emoji,
                anime_title=topic_cfg.title,
                episode_number=episode,
            )
            if target_msg_ids:
                await self.publisher.send_success_confirmation(
                    update, target_msg_ids, topic_cfg.emoji, topic_cfg.title, episode
                )
        except Exception as e:
            logger.error(f"Auto-publish failed: {e}")
            await self.publisher.send_admin_warning(update, f"❌ Publishing failed. Use /retry to retry.")

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query: CallbackQuery = update.callback_query
        if not query:
            return
        if not self._is_admin(query.from_user.id):
            await query.answer("❌ Unauthorized.", show_alert=True)
            return

        data = query.data
        if not data:
            return

        parts = data.split("_", 1)
        if len(parts) != 2:
            return

        action, item_id_str = parts
        try:
            item_id = int(item_id_str)
        except ValueError:
            return

        if action == "publish":
            await self._callback_publish(query, item_id)
        elif action == "edit":
            await self._callback_edit(query, item_id)
        elif action == "cancel":
            await self._callback_cancel(query, item_id)

    async def _callback_publish(self, query: CallbackQuery, item_id: int) -> None:
        item = await self.db.get_by_id(item_id)
        if not item:
            await query.answer("Item not found.", show_alert=True)
            return

        if item["status"] == "published":
            await query.answer("Already published.", show_alert=True)
            return

        if item["status"] == "publishing":
            await query.answer("Already publishing.", show_alert=True)
            return

        if item["status"] not in ("pending", "failed"):
            await query.answer(f"Cannot publish from status: {item['status']}", show_alert=True)
            return

        target_chat_id = self.config.target_channel
        target_msg_ids = await self.publisher.publish_item(
            item_id=item_id,
            source_chat_id=item["source_chat_id"],
            source_message_id=item["source_message_id"],
            media_type=item["media_type"],
            file_id=item["file_id"],
            caption=item.get("caption"),
            target_chat_id=target_chat_id,
            emoji=item.get("emoji", ""),
            anime_title=item.get("anime_title", ""),
            episode_number=item.get("episode_number", ""),
        )
        if target_msg_ids:
            await query.edit_message_text(
                f"✅ Published Successfully!\n\n"
                f"{item.get('emoji','')} {item.get('anime_title','')}\n"
                f"Episode: {item.get('episode_number','?')}\n"
                f"@aniwavebd",
                reply_markup=None,
            )
            await query.answer()
        else:
            await query.edit_message_text(
                "Publishing failed. Use /retry to try again.",
                reply_markup=None,
            )
            await query.answer()

    async def _callback_edit(self, query: CallbackQuery, item_id: int) -> None:
        item = await self.db.get_by_id(item_id)
        if not item:
            await query.answer("Item not found.", show_alert=True)
            return

        text = (
            "✏️ **Edit Episode Number**\n\n"
            "Send the new episode number as a message.\n\n"
            f"Current episode: {item.get('episode_number', 'None')}"
        )
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=None)
        await query.answer()

    async def _callback_cancel(self, query: CallbackQuery, item_id: int) -> None:
        await self.db.update_status(item_id, "cancelled")
        await query.edit_message_text(
            "❌ **Cancelled.** Item has been cancelled.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=None,
        )
        await query.answer()

    async def handle_retry(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update.effective_user.id):
            return
        message = update.message
        args = context.args
        if not args:
            await message.reply_text("Usage: /retry <item_id>")
            return
        try:
            item_id = int(args[0])
        except ValueError:
            await message.reply_text("Invalid item ID.")
            return

        item = await self.db.get_by_id(item_id)
        if not item:
            await message.reply_text("Item not found.")
            return
        if item["status"] == "published":
            await message.reply_text("Already published.")
            return

        target_chat_id = self.config.target_channel
        target_msg_ids = await self.publisher.publish_item(
            item_id=item_id,
            source_chat_id=item["source_chat_id"],
            source_message_id=item["source_message_id"],
            media_type=item["media_type"],
            file_id=item["file_id"],
            caption=item.get("caption"),
            target_chat_id=target_chat_id,
            emoji=item.get("emoji", ""),
            anime_title=item.get("anime_title", ""),
            episode_number=item.get("episode_number", ""),
        )
        if target_msg_ids:
            await message.reply_text(f"✅ Retry published! Target IDs: {target_msg_ids}")
        else:
            await message.reply_text("❌ Retry failed.")

    async def handle_edit_episode_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        pass

    def get_handlers(self) -> list:
        return [
            CommandHandler("start", self.handle_start),
            CommandHandler("help", self.handle_help),
            CommandHandler("status", self.handle_status),
            CommandHandler("chatid", self.handle_chatid),
            CommandHandler("topicid", self.handle_topicid),
            CommandHandler("debug", self.handle_debug),
            CommandHandler("retry", self.handle_retry),
            MessageHandler(
                ft.ALL,
                self.handle_media,
            ),
            CallbackQueryHandler(self.handle_callback),
        ]
