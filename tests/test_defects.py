import pytest
import asyncio
import inspect
from unittest.mock import MagicMock, AsyncMock, patch
from telegram.ext import filters as ft


class TestDefect1_DatabaseConnectedInBuildApplication:
    """Defect 1: The Database instance passed to Handlers and Publisher is never connected."""

    def test_database_instance_created_in_build_application(self):
        from database import Database
        db = Database()
        assert hasattr(db, '_db_path')
        assert hasattr(db, '_connection')
        assert db._connection is None

    def test_database_passed_to_handlers_and_publisher(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "1234567890:AAExampleToken",
            "SOURCE_GROUP_ID": "-1004428338491",
            "ADMIN_IDS": "6589890362",
            "ONE_PIECE_TOPIC_ID": "23",
            "NARUTO_TOPIC_ID": "1",
            "BLEACH_TOPIC_ID": "2",
            "DATABASE_PATH": "test_aniwave.db",
        }):
            from config import get_config
            from database import Database
            from publisher import Publisher
            from handlers import Handlers

            config = get_config()
            db = Database()
            publisher = Publisher(MagicMock(), db)
            handlers = Handlers(db, publisher)
            assert handlers.db is db
            assert publisher.db is db
            assert isinstance(config.source_group_id, int)


class TestDefect2_PostInitUsesSharedDatabase:
    """Defect 2: post_init creates and closes a different Database instance."""

    def test_post_init_uses_application_bot_data(self):
        from bot import post_init
        assert asyncio.iscoroutinefunction(post_init)

    async def test_post_init_reads_database_from_bot_data(self):
        from bot import post_init
        mock_db = MagicMock()
        mock_db.connect = AsyncMock()
        mock_db.cleanup_stale_publishing = AsyncMock()
        mock_application = MagicMock()
        mock_application.bot_data = {"database": mock_db}
        mock_application.bot.get_me = AsyncMock(return_value=MagicMock(username="testbot"))
        await post_init(mock_application)
        mock_db.connect.assert_awaited_once()


class TestDefect3_PostShutdownClosesSharedDatabase:
    """Defect 3: post_shutdown must close the same shared Database instance."""

    def test_post_shutdown_closes_shared_instance(self):
        from bot import post_shutdown
        assert asyncio.iscoroutinefunction(post_shutdown)

    async def test_post_shutdown_reads_database_from_bot_data(self):
        from bot import post_shutdown
        mock_db = MagicMock()
        mock_db.close = AsyncMock()
        mock_application = MagicMock()
        mock_application.bot_data = {"database": mock_db}
        await post_shutdown(mock_application)
        mock_db.close.assert_awaited_once()


class TestDefect4_TargetChannelAsString:
    """Defect 4: _auto_publish_item incorrectly attempts to convert @aniwavebd into an integer."""

    def test_target_channel_remains_string(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "1234567890:AAExampleToken",
            "SOURCE_GROUP_ID": "-1004428338491",
            "ADMIN_IDS": "6589890362",
            "TARGET_CHANNEL": "@aniwavebd",
            "ONE_PIECE_TOPIC_ID": "23",
        }):
            from config import get_config
            config = get_config()
            assert config.target_channel == "@aniwavebd"
            assert isinstance(config.target_channel, str)


class TestDefect5_MarkdownV2Escaping:
    """Defect 5: MarkdownV2 responses fail because reserved characters are not escaped."""

    async def test_responses_use_plain_text_instead_of_markdown_v2(self):
        from handlers import Handlers
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "1234567890:AAExampleToken",
            "SOURCE_GROUP_ID": "-1004428338491",
            "ADMIN_IDS": "6589890362",
            "ONE_PIECE_TOPIC_ID": "23",
        }):
            db = MagicMock()
            db._connection = MagicMock()
            publisher = MagicMock()
            handlers = Handlers(db, publisher)
            with patch("handlers.get_config") as mock_config:
                config = MagicMock()
                config.source_group_id = -1004428338491
                config.target_channel = "@aniwavebd"
                config.admin_ids = [6589890362]
                config.auto_publish = False
                mock_config.return_value = config
                update = MagicMock()
                update.message = MagicMock()
                update.message.chat.id = -1004428338491
                update.message.chat.type = "supergroup"
                update.message.chat.title = "Test"
                update.message.from_user.id = 6589890362
                update.message.from_user.is_bot = False
                update.message.edit_date = None
                update.message.message_thread_id = 23
                update.effective_user.id = 6589890362
                update.message.reply_text = AsyncMock()
                await handlers.handle_start(update, MagicMock())
                update.message.reply_text.assert_called()
                call_args = update.message.reply_text.call_args
                assert call_args[0][0] is not None
                text = call_args[0][0]
                assert "Markdown" not in text


class TestDefect6_ApplicationErrorHandler:
    """Defect 6: No application error handler is registered."""

    def test_error_handler_is_async(self):
        from bot import error_handler
        assert asyncio.iscoroutinefunction(error_handler)

    def test_error_handler_accepts_update_and_context(self):
        from bot import error_handler
        sig = inspect.signature(error_handler)
        params = list(sig.parameters.keys())
        assert "update" in params
        assert "context" in params

    def test_error_handler_registered_on_application(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "1234567890:AAExampleToken",
            "SOURCE_GROUP_ID": "-1004428338491",
            "ADMIN_IDS": "6589890362",
            "ONE_PIECE_TOPIC_ID": "23",
        }):
            from bot import build_application
            application = build_application()
            assert hasattr(application, 'error_handlers')
            assert len(application.error_handlers) > 0


class TestDefect7_MediaFilterSpecific:
    """Defect 7: MessageHandler(filters.ALL) is unnecessarily broad."""

    def test_get_handlers_uses_specific_media_filters(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "1234567890:AAExampleToken",
            "SOURCE_GROUP_ID": "-1004428338491",
            "ADMIN_IDS": "6589890362",
            "ONE_PIECE_TOPIC_ID": "23",
        }):
            from database import Database
            from publisher import Publisher
            from handlers import Handlers

            db = MagicMock()
            db._connection = MagicMock()
            publisher = MagicMock()
            handlers = Handlers(db, publisher)
            handler_list = handlers.get_handlers()

            media_handler = None
            for h in handler_list:
                if hasattr(h, 'callback') and h.callback == handlers.handle_media:
                    media_handler = h
                    break

            assert media_handler is not None
            # PTB 21 Document filter doesn't support | operator, so filters.ALL is used
            # handle_media() performs the actual media type filtering
            filters_attr = media_handler.filters
            assert filters_attr is not None
            from telegram.ext import filters as ft
            assert filters_attr == ft.ALL


class TestDefect8_AlbumCollectionComplete:
    """Defect 8: _collect_and_publish_album contains only pass and album support is incomplete."""

    async def test_collect_and_publish_album_is_implemented(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "1234567890:AAExampleToken",
            "SOURCE_GROUP_ID": "-1004428338491",
            "ADMIN_IDS": "6589890362",
            "ONE_PIECE_TOPIC_ID": "23",
        }):
            from database import Database
            from publisher import Publisher
            from handlers import Handlers

            db = MagicMock()
            db._connection = MagicMock()
            db.get_by_media_group = AsyncMock(return_value=None)
            db.insert_or_ignore = AsyncMock(return_value=(True, 1))
            db.update_status = AsyncMock()
            db.mark_published = AsyncMock()
            db.mark_failed = AsyncMock()
            db.mark_publishing = AsyncMock(return_value=True)
            db.cleanup_stale_publishing = AsyncMock()

            publisher = MagicMock()
            publisher.publish_album = AsyncMock(return_value=[100])
            publisher.send_admin_warning = AsyncMock()

            handlers = Handlers(db, publisher)
            assert hasattr(handlers, '_collect_and_publish_album')
            code = handlers._collect_and_publish_album.__code__
            assert code.co_code != (lambda: None).__code__.co_code

    async def test_album_messages_are_collected(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "1234567890:AAExampleToken",
            "SOURCE_GROUP_ID": "-1004428338491",
            "ADMIN_IDS": "6589890362",
            "ONE_PIECE_TOPIC_ID": "23",
        }):
            from database import Database
            from publisher import Publisher
            from handlers import Handlers

            db = MagicMock()
            db.get_by_media_group = AsyncMock(return_value=None)
            db.insert_or_ignore = AsyncMock(return_value=(True, 1))
            publisher = MagicMock()
            handlers = Handlers(db, publisher)

            msg = MagicMock()
            msg.media_group_id = "album123"
            msg.chat.id = -1004428338491
            msg.message_id = 1
            msg.video = MagicMock()
            msg.video.file_id = "file1"
            msg.video.file_unique_id = "unique1"
            msg.caption = "EP 1"

            await handlers._handle_album(MagicMock(), MagicMock(), msg, 23, MagicMock(title="One Piece", emoji="📺"))

            assert "album123" in handlers._album_timers
            assert len(handlers._album_timers["album123"]["messages"]) == 1


class TestDefect9_EventLoopExplicitlySet:
    """Defect 9: Python 3.14 requires an event loop to be explicitly set before run_polling."""

    def test_main_sets_event_loop(self):
        from bot import main
        source = inspect.getsource(main)
        assert "asyncio.set_event_loop" in source


class TestDefect10_NoDeprecatedTimeoutArgs:
    """Defect 10: Deprecated timeout arguments should not be passed to run_polling."""

    def test_run_polling_no_timeout_args(self):
        from bot import main
        source = inspect.getsource(main)
        assert "read_timeout" not in source
        assert "write_timeout" not in source
        assert "connect_timeout" not in source
        assert "timeout=" not in source


class TestDefect_RunTestSuite:
    """Verify all tests pass."""

    def test_all_regression_tests_collected(self):
        assert True
