import logging
from typing import List, Optional

from telegram import Bot, InputMedia, InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAnimation, InputMediaAudio, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.constants import ParseMode

from config import get_config
from database import Database
from caption import generate_caption, parse_episode

logger = logging.getLogger(__name__)


class Publisher:
    def __init__(self, bot: Bot, database: Database) -> None:
        self.bot = bot
        self.db = database
        self.config = get_config()

    async def copy_single_media(
        self,
        source_chat_id: int,
        source_message_id: int,
        target_chat_id: str,
        media_type: str,
        file_id: str,
        caption: Optional[str],
    ) -> Optional[int]:
        try:
            kwargs = {"caption": caption, "parse_mode": ParseMode.MARKDOWN_V2} if caption else {}
            msg = await self.bot.copy_message(
                chat_id=target_chat_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id,
                **kwargs,
            )
            return msg.message_id
        except TelegramError as e:
            logger.error(f"Failed to copy media {source_message_id}: {e}")
            return None

    async def publish_album(
        self,
        source_chat_id: int,
        message_ids: List[int],
        target_chat_id: str,
        media_items: List[dict],
    ) -> Optional[List[int]]:
        try:
            media_group: List[InputMedia] = []
            for item in media_items:
                mt = item["media_type"]
                fi = item["file_id"]
                caption = item.get("caption")
                if mt == "photo":
                    media_group.append(InputMediaPhoto(media=fi, caption=caption, parse_mode=ParseMode.MARKDOWN_V2))
                elif mt in ("video", "animation", "audio", "document"):
                    media_group.append(InputMedia(media=fi, type=mt.upper(), caption=caption, parse_mode=ParseMode.MARKDOWN_V2))
            sent = await self.bot.send_media_group(
                chat_id=target_chat_id,
                media=media_group,
            )
            return [m.message_id for m in sent]
        except TelegramError as e:
            logger.error(f"Failed to publish album: {e}")
            return None

    async def publish_item(
        self,
        item_id: int,
        source_chat_id: int,
        source_message_id: int,
        media_type: str,
        file_id: str,
        caption: Optional[str],
        target_chat_id: str,
        emoji: str = "",
        anime_title: str = "",
        episode_number: str = "",
    ) -> Optional[List[int]]:
        if not await self.db.mark_publishing(item_id):
            logger.warning(f"Item {item_id} could not be marked as publishing (already processing)")
            return None

        target_msg_ids: Optional[List[int]] = None
        try:
            final_caption = ""
            if caption:
                ep = parse_episode(caption)
                if ep:
                    episode_number = ep
                if emoji and anime_title and episode_number:
                    final_caption = generate_caption(emoji, anime_title, episode_number, self.config.include_hd_claim)
                    final_caption = final_caption[:1024]
                else:
                    final_caption = caption[:1024]
            else:
                final_caption = ""

            if media_type in ("photo", "video", "animation", "audio", "document"):
                msg_id = await self.copy_single_media(
                    source_chat_id, source_message_id, target_chat_id, media_type, file_id, final_caption or None
                )
                target_msg_ids = [msg_id] if msg_id else None

            if target_msg_ids:
                await self.db.mark_published(
                    item_id=item_id,
                    target_chat_id=target_chat_id,
                    target_message_ids=[str(m) for m in target_msg_ids],
                    final_caption=final_caption,
                )
                return target_msg_ids
            else:
                await self.db.mark_failed(item_id)
                return None
        except Exception as e:
            logger.error(f"Publish error for item {item_id}: {e}")
            await self.db.mark_failed(item_id)
            return None

    async def retry_publish(self, item_id: int) -> Optional[List[int]]:
        item = await self.db.get_by_id(item_id)
        if not item:
            return None
        return await self.publish_item(
            item_id=item_id,
            source_chat_id=item["source_chat_id"],
            source_message_id=item["source_message_id"],
            media_type=item["media_type"],
            file_id=item["file_id"],
            caption=item.get("caption"),
            target_chat_id=self.config.target_channel,
            emoji=item.get("emoji", ""),
            anime_title=item.get("anime_title", ""),
            episode_number=item.get("episode_number", ""),
        )

    async def send_preview(
        self,
        update,
        item_id: int,
        emoji: str,
        anime_title: str,
        episode_number: str,
        media_type: str,
    ) -> None:
        try:
            text = (
                f"Pending Approval\n\n"
                f"Topic: {emoji} {anime_title}\n"
                f"Episode: {episode_number or '?'}\n"
                f"Type: {media_type}\n\n"
                f"Press the button below to publish."
            )
            buttons = [
                [InlineKeyboardButton("Publish", callback_data=f"publish_{item_id}")],
                [InlineKeyboardButton("Edit Episode", callback_data=f"edit_{item_id}")],
                [InlineKeyboardButton("Cancel", callback_data=f"cancel_{item_id}")],
            ]
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        except TelegramError as e:
            logger.error(f"Failed to send preview: {e}")

    async def send_admin_warning(self, update, text: str) -> None:
        try:
            await update.message.reply_text(text)
        except TelegramError as e:
            logger.error(f"Failed to send warning: {e}")

    async def send_success_confirmation(self, update, target_message_ids: List[int], emoji: str, anime_title: str, episode_number: str) -> None:
        try:
            text = (
                f"Published Successfully\n\n"
                f"{emoji} {anime_title}\n"
                f"Episode: {episode_number}\n"
                f"@aniwavebd"
            )
            await update.message.reply_text(text)
        except TelegramError as e:
            logger.error(f"Failed to send success: {e}")

    async def get_item_by_id(self, item_id: int) -> Optional[dict]:
        return await self.db.get_by_id(item_id)
