import pytest
from database import Database


class TestDatabase:
    async def test_insert_and_get_by_source(self, db):
        item = {
            "source_chat_id": -1001234567890,
            "source_message_id": 42,
            "media_group_id": None,
            "topic_id": 1,
            "sender_id": 6589890362,
            "media_type": "video",
            "file_id": "file123",
            "file_unique_id": "unique123",
            "caption": "EP 100",
            "anime_title": "One Piece",
            "emoji": "📺",
            "episode_number": "100",
            "status": "pending",
            "final_caption": "",
        }
        inserted, item_id = await db.insert_or_ignore(item)
        assert inserted is True
        assert item_id > 0

        found = await db.get_by_source(-1001234567890, 42)
        assert found is not None
        assert found["source_message_id"] == 42
        assert found["status"] == "pending"

    async def test_duplicate_insert_ignored(self, db):
        item = {
            "source_chat_id": -1001234567890,
            "source_message_id": 42,
            "media_group_id": None,
            "topic_id": 1,
            "sender_id": 6589890362,
            "media_type": "video",
            "file_id": "file123",
            "file_unique_id": "unique123",
            "caption": None,
            "anime_title": "",
            "emoji": "",
            "episode_number": "",
            "status": "pending",
            "final_caption": "",
        }
        inserted1, _ = await db.insert_or_ignore(item)
        inserted2, _ = await db.insert_or_ignore(item)
        assert inserted1 is True
        assert inserted2 is False

    async def test_update_status(self, db):
        item = {
            "source_chat_id": -1001234567890,
            "source_message_id": 1,
            "media_group_id": None,
            "topic_id": 1,
            "sender_id": 6589890362,
            "media_type": "photo",
            "file_id": "f1",
            "file_unique_id": "u1",
            "caption": None,
            "anime_title": "",
            "emoji": "",
            "episode_number": "",
            "status": "pending",
            "final_caption": "",
        }
        inserted, item_id = await db.insert_or_ignore(item)
        assert inserted
        await db.update_status(item_id, "published", target_chat_id="@aniwavebd", target_message_id="100", published_at="2024-01-01")
        found = await db.get_by_source(-1001234567890, 1)
        assert found["status"] == "published"

    async def test_mark_publishing(self, db):
        item = {
            "source_chat_id": -1001234567890,
            "source_message_id": 1,
            "media_group_id": None,
            "topic_id": 1,
            "sender_id": 6589890362,
            "media_type": "photo",
            "file_id": "f1",
            "file_unique_id": "u1",
            "caption": None,
            "anime_title": "",
            "emoji": "",
            "episode_number": "",
            "status": "pending",
            "final_caption": "",
        }
        inserted, item_id = await db.insert_or_ignore(item)
        assert inserted
        result = await db.mark_publishing(item_id)
        assert result is True
        found = await db.get_by_source(-1001234567890, 1)
        assert found["status"] == "publishing"

    async def test_mark_publishing_already_processing(self, db):
        item = {
            "source_chat_id": -1001234567890,
            "source_message_id": 1,
            "media_group_id": None,
            "topic_id": 1,
            "sender_id": 6589890362,
            "media_type": "photo",
            "file_id": "f1",
            "file_unique_id": "u1",
            "caption": None,
            "anime_title": "",
            "emoji": "",
            "episode_number": "",
            "status": "publishing",
            "final_caption": "",
        }
        inserted, item_id = await db.insert_or_ignore(item)
        result = await db.mark_publishing(item_id)
        assert result is False

    async def test_mark_published(self, db):
        item = {
            "source_chat_id": -1001234567890,
            "source_message_id": 1,
            "media_group_id": None,
            "topic_id": 1,
            "sender_id": 6589890362,
            "media_type": "photo",
            "file_id": "f1",
            "file_unique_id": "u1",
            "caption": None,
            "anime_title": "",
            "emoji": "",
            "episode_number": "",
            "status": "pending",
            "final_caption": "",
        }
        inserted, item_id = await db.insert_or_ignore(item)
        await db.mark_published(item_id, "@aniwavebd", ["100"], "test caption")
        found = await db.get_by_source(-1001234567890, 1)
        assert found["status"] == "published"
        assert found["target_message_id"] == "100"

    async def test_mark_failed(self, db):
        item = {
            "source_chat_id": -1001234567890,
            "source_message_id": 1,
            "media_group_id": None,
            "topic_id": 1,
            "sender_id": 6589890362,
            "media_type": "photo",
            "file_id": "f1",
            "file_unique_id": "u1",
            "caption": None,
            "anime_title": "",
            "emoji": "",
            "episode_number": "",
            "status": "pending",
            "final_caption": "",
        }
        inserted, item_id = await db.insert_or_ignore(item)
        await db.mark_failed(item_id)
        found = await db.get_by_source(-1001234567890, 1)
        assert found["status"] == "failed"

    async def test_mark_cancelled(self, db):
        item = {
            "source_chat_id": -1001234567890,
            "source_message_id": 1,
            "media_group_id": None,
            "topic_id": 1,
            "sender_id": 6589890362,
            "media_type": "photo",
            "file_id": "f1",
            "file_unique_id": "u1",
            "caption": None,
            "anime_title": "",
            "emoji": "",
            "episode_number": "",
            "status": "pending",
            "final_caption": "",
        }
        inserted, item_id = await db.insert_or_ignore(item)
        await db.mark_cancelled(item_id)
        found = await db.get_by_source(-1001234567890, 1)
        assert found["status"] == "cancelled"

    async def test_count_pending(self, db):
        item = {
            "source_chat_id": -1001234567890,
            "source_message_id": 1,
            "media_group_id": None,
            "topic_id": 1,
            "sender_id": 6589890362,
            "media_type": "photo",
            "file_id": "f1",
            "file_unique_id": "u1",
            "caption": None,
            "anime_title": "",
            "emoji": "",
            "episode_number": "",
            "status": "pending",
            "final_caption": "",
        }
        await db.insert_or_ignore(item)
        count = await db.count_pending()
        assert count >= 1

    async def test_get_by_id(self, db):
        item = {
            "source_chat_id": -1001234567890,
            "source_message_id": 1,
            "media_group_id": None,
            "topic_id": 1,
            "sender_id": 6589890362,
            "media_type": "photo",
            "file_id": "f1",
            "file_unique_id": "u1",
            "caption": None,
            "anime_title": "",
            "emoji": "",
            "episode_number": "",
            "status": "pending",
            "final_caption": "",
        }
        inserted, item_id = await db.insert_or_ignore(item)
        assert inserted
        found = await db.get_by_id(item_id)
        assert found is not None
        assert found["id"] == item_id

    async def test_update_episode(self, db):
        item = {
            "source_chat_id": -1001234567890,
            "source_message_id": 1,
            "media_group_id": None,
            "topic_id": 1,
            "sender_id": 6589890362,
            "media_type": "photo",
            "file_id": "f1",
            "file_unique_id": "u1",
            "caption": None,
            "anime_title": "",
            "emoji": "",
            "episode_number": "",
            "status": "pending",
            "final_caption": "",
        }
        inserted, item_id = await db.insert_or_ignore(item)
        await db.update_episode(item_id, "1165")
        found = await db.get_by_source(-1001234567890, 1)
        assert found["episode_number"] == "1165"

    async def test_get_all_configured_topic_ids(self, db):
        from config import TOPIC_MAP
        topic_ids = await db.get_all_configured_topic_ids()
        assert isinstance(topic_ids, list)
        for tid in topic_ids:
            assert tid in TOPIC_MAP