import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from telegram import Update, Message, Chat, User
from telegram.ext import ContextTypes


class FakeMessage:
    def __init__(self, **kwargs):
        self.chat = MagicMock(spec=Chat)
        self.chat.id = kwargs.get("chat_id", -1001234567890)
        self.chat.title = "AniWave Database"
        self.chat.type = "supergroup"
        self.from_user = MagicMock(spec=User)
        self.from_user.id = kwargs.get("sender_id", 6589890362)
        self.from_user.is_bot = False
        self.message_id = kwargs.get("message_id", 1)
        self.message_thread_id = kwargs.get("message_thread_id", None)
        self.edit_date = kwargs.get("edit_date", None)
        self.caption = kwargs.get("caption", None)
        self.media_group_id = kwargs.get("media_group_id", None)
        self.video = kwargs.get("video", None)
        self.document = kwargs.get("document", None)
        self.animation = kwargs.get("animation", None)
        self.audio = kwargs.get("audio", None)
        self.photo = kwargs.get("photo", None)
        self.service_message = False


class TestHandlers:
    @pytest.fixture
    def handlers(self):
        db = MagicMock()
        db._connection = MagicMock()
        publisher = MagicMock()
        publisher.bot = MagicMock()
        with patch("handlers.get_config") as mock_config:
            config = MagicMock()
            config.source_group_id = -1001234567890
            config.target_channel = "@aniwavebd"
            config.admin_ids = [6589890362]
            config.auto_publish = False
            config.include_hd_claim = False
            mock_config.return_value = config
            from handlers import Handlers
            return Handlers(db, publisher)

    def test_is_admin_true(self, handlers):
        assert handlers._is_admin(6589890362) is True

    def test_is_admin_false(self, handlers):
        assert handlers._is_admin(999999) is False

    def test_is_bot_message_true(self, handlers):
        msg = MagicMock(spec=Message)
        msg.from_user = MagicMock()
        msg.from_user.is_bot = True
        assert handlers._is_bot_message(msg) is True

    def test_is_bot_message_false(self, handlers):
        msg = MagicMock(spec=Message)
        msg.from_user = MagicMock()
        msg.from_user.is_bot = False
        assert handlers._is_bot_message(msg) is False

    def test_is_configured_topic_true(self, handlers):
        from config import TOPIC_MAP, _build_topic_map
        _build_topic_map()
        for tid in TOPIC_MAP:
            assert handlers._is_configured_topic(tid) is True

    def test_is_configured_topic_false(self, handlers):
        assert handlers._is_configured_topic(99999) is False
        assert handlers._is_configured_topic(None) is False

    def test_has_media_true_with_photo(self, handlers):
        msg = MagicMock(spec=Message)
        msg.photo = [MagicMock()]
        assert handlers._has_media(msg) is True

    def test_has_media_true_with_video(self, handlers):
        msg = MagicMock(spec=Message)
        msg.video = MagicMock()
        assert handlers._has_media(msg) is True

    def test_has_media_false(self, handlers):
        msg = MagicMock(spec=Message)
        msg.photo = None
        msg.video = None
        msg.document = None
        msg.animation = None
        msg.audio = None
        msg.media_group_id = None
        assert handlers._has_media(msg) is False

    def test_get_topic_id(self, handlers):
        update = MagicMock(spec=Update)
        update.message = MagicMock()
        update.message.message_thread_id = 123
        assert handlers._get_topic_id(update) == 123

    def test_get_topic_id_none(self, handlers):
        update = MagicMock(spec=Update)
        update.message = MagicMock()
        update.message.message_thread_id = None
        assert handlers._get_topic_id(update) is None

    def test_should_process_non_supergroup(self, handlers):
        update = MagicMock(spec=Update)
        msg = MagicMock(spec=Message)
        msg.chat.type = "private"
        update.message = msg
        assert handlers._should_process(update) is False

    def test_should_process_wrong_chat(self, handlers):
        update = MagicMock(spec=Update)
        msg = MagicMock(spec=Message)
        msg.chat.type = "supergroup"
        msg.chat.id = -999999999
        update.message = msg
        assert handlers._should_process(update) is False

    def test_should_process_bot_user(self, handlers):
        update = MagicMock(spec=Update)
        msg = MagicMock(spec=Message)
        msg.chat.type = "supergroup"
        msg.chat.id = -1001234567890
        msg.from_user = MagicMock()
        msg.from_user.is_bot = True
        msg.edit_date = None
        update.message = msg
        assert handlers._should_process(update) is False

    def test_should_process_edited(self, handlers):
        update = MagicMock(spec=Update)
        msg = MagicMock(spec=Message)
        msg.chat.type = "supergroup"
        msg.chat.id = -1001234567890
        msg.from_user = MagicMock()
        msg.from_user.is_bot = False
        msg.edit_date = "2024-01-01"
        update.message = msg
        assert handlers._should_process(update) is False

    def test_should_process_non_admin(self, handlers):
        update = MagicMock(spec=Update)
        msg = MagicMock(spec=Message)
        msg.chat.type = "supergroup"
        msg.chat.id = -1001234567890
        msg.from_user = MagicMock()
        msg.from_user.is_bot = False
        msg.from_user.id = 999999
        msg.edit_date = None
        update.message = msg
        assert handlers._should_process(update) is False

    def test_should_process_valid(self, handlers):
        update = MagicMock(spec=Update)
        msg = MagicMock(spec=Message)
        msg.chat.type = "supergroup"
        msg.chat.id = -1001234567890
        msg.from_user = MagicMock()
        msg.from_user.is_bot = False
        msg.from_user.id = 6589890362
        msg.edit_date = None
        msg.message_thread_id = 1
        update.message = msg
        assert handlers._should_process(update) is True

    def test_handle_start_not_admin(self, handlers):
        update = MagicMock(spec=Update)
        update.effective_user.id = 999
        update.message = MagicMock()
        # Should not raise, just return without sending

    def test_get_handlers_returns_list(self, handlers):
        handler_list = handlers.get_handlers()
        assert isinstance(handler_list, list)
        assert len(handler_list) > 0
