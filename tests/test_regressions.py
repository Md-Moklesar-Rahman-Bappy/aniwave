"""Regression tests for the ten originally confirmed defects.

Each test names the defect it prevents from coming back. The original code is
gone, so these assert the *fixed* behaviour rather than the old internals.
"""

from __future__ import annotations

import asyncio
import inspect
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.ext import filters

import ui
from config import AppConfig
from database import Database, Status
from handlers import SUPPORTED_MEDIA
from publisher import build_album_media
from tests.conftest import ADMIN_ID, SOURCE_CHAT
from tests.test_database import make_album, make_item
from tests.test_handlers import FakeContext, FakeQuery, make_update


# --------------------------------------------------------------------------- #
# Defect 1/2/3 - one shared Database, connected in post_init, closed in post_shutdown
# --------------------------------------------------------------------------- #


async def test_defect1_single_shared_database_instance(env, tmp_path):
    """The Database handed to Publisher/Handlers must be the connected one."""
    import bot as bot_module

    values = dict(env)
    values["DATABASE_PATH"] = str(tmp_path / "shared.db")
    application = bot_module.build_application(AppConfig.load(values))

    database = application.bot_data["database"]
    assert database is application.bot_data["handlers"].db
    assert database is application.bot_data["publisher"].db
    assert database is application.bot_data["albums"].db
    assert application.bot_data["albums"].publisher.db is database


async def test_defect2_post_init_uses_bot_data_instance(env, tmp_path, fake_bot):
    """post_init must not construct its own Database."""
    import bot as bot_module

    application, database = _lifecycle_app(env, tmp_path, fake_bot)
    await bot_module.post_init(application)
    try:
        assert database.is_connected is True
    finally:
        await bot_module.post_shutdown(application)


async def test_defect3_post_shutdown_closes_same_instance(env, tmp_path, fake_bot):
    import bot as bot_module

    application, database = _lifecycle_app(env, tmp_path, fake_bot)
    await bot_module.post_init(application)
    await bot_module.post_shutdown(application)
    assert database.is_connected is False


def _lifecycle_app(env, tmp_path, fake_bot):
    import bot as bot_module
    from telegram.ext import ApplicationBuilder

    from albums import AlbumService
    from publisher import Publisher

    values = dict(env)
    values["DATABASE_PATH"] = str(tmp_path / "life.db")
    config = AppConfig.load(values)
    application = ApplicationBuilder().token(config.bot_token).build()
    application.bot_data["config"] = config
    database = Database(config.database_path)
    application.bot_data["database"] = database
    application.bot_data["publisher"] = Publisher(fake_bot, database, config)
    application.bot_data["albums"] = AlbumService(database, application.bot_data["publisher"],
                                                 fake_bot, config)
    application.bot = fake_bot
    return application, database


# --------------------------------------------------------------------------- #
# Defect 4 - TARGET_CHANNEL must stay a string
# --------------------------------------------------------------------------- #


async def test_defect4_target_channel_never_converted_to_int(env):
    config = AppConfig.load({**env, "TARGET_CHANNEL": "@aniwavebd"})
    assert config.target_channel == "@aniwavebd"
    assert isinstance(config.target_channel, str)
    with pytest.raises(ValueError):
        int(config.target_channel)


async def test_defect4_publisher_passes_string_target(publisher, db, fake_bot):
    _, row_id = await make_item(db)
    await publisher.publish_item_row(row_id)
    assert fake_bot.copy_calls[0]["chat_id"] == "@aniwavebd"


# --------------------------------------------------------------------------- #
# Defect 5 - MarkdownV2 escaping failures
# --------------------------------------------------------------------------- #


async def test_defect5_captions_are_plain_text(publisher, db, fake_bot):
    _, row_id = await make_item(db, episode_number="12.5")
    await publisher.publish_item_row(row_id)
    call = fake_bot.copy_calls[0]
    assert call["parse_mode"] is None
    # The caption contains characters MarkdownV2 reserves; it must still be sent.
    assert "." in call["caption"] and "@" in call["caption"]


def test_defect5_no_markdown_v2_in_source():
    for module in ("publisher.py", "handlers.py", "ui.py", "albums.py", "bot.py"):
        source = (Path(__file__).resolve().parent.parent / module).read_text(encoding="utf-8")
        assert "MARKDOWN_V2" not in source, f"{module} still references MarkdownV2"


async def test_defect5_previews_are_plain_text(handlers, db):
    update = make_update(media={"video": True}, caption="EP 1165")
    await handlers.handle_media(update, FakeContext())
    text = " ".join(update.message.reply.texts)
    assert "**" not in text
    assert update.message.reply.markup is not None


# --------------------------------------------------------------------------- #
# Defect 6 - application error handler
# --------------------------------------------------------------------------- #


async def test_defect6_error_handler_registered(env, tmp_path):
    import bot as bot_module

    values = dict(env)
    values["DATABASE_PATH"] = str(tmp_path / "eh.db")
    application = bot_module.build_application(AppConfig.load(values))
    # PTB 21 stores error handlers as a {callback: <truthy>} mapping.
    assert bot_module.error_handler in application.error_handlers
    assert len(application.error_handlers) == 1


async def test_defect6_error_handler_is_async():
    import bot as bot_module

    assert inspect.iscoroutinefunction(bot_module.error_handler)


# --------------------------------------------------------------------------- #
# Defect 7 - filters.ALL was too broad
# --------------------------------------------------------------------------- #


async def test_defect7_media_filter_is_not_filters_all():
    assert SUPPORTED_MEDIA is not filters.ALL


async def test_defect7_media_filter_rejects_plain_text():
    message = SimpleNamespace(video=None, animation=None, audio=None, photo=None,
                              document=None, media_group_id=None, text="EP 1165")
    assert SUPPORTED_MEDIA.filter(message) is False


async def test_defect7_media_filter_accepts_each_supported_type():
    for attribute in ("video", "animation", "audio", "photo", "document"):
        message = SimpleNamespace(video=None, animation=None, audio=None, photo=None,
                                  document=None, media_group_id=None, text=None)
        setattr(message, attribute, object())
        assert SUPPORTED_MEDIA.filter(message) is True


def test_defect7_no_module_uses_filters_all():
    root = Path(__file__).resolve().parent.parent
    for module in ("handlers.py", "albums.py", "bot.py"):
        source = (root / module).read_text(encoding="utf-8")
        assert "filters.ALL" not in source
        assert "ft.ALL" not in source


# --------------------------------------------------------------------------- #
# Defect 8 - album collector was a `pass` stub
# --------------------------------------------------------------------------- #


def test_defect8_album_collector_is_implemented():
    from albums import AlbumService

    source = inspect.getsource(AlbumService.finalize_album)
    assert source.strip() != ""
    assert "pass" not in source


async def test_defect8_album_entry_is_read_before_removal(album_service, db, clock):
    """The original bug popped the in-memory entry before reading it."""
    album_id = await album_service.ingest_part(
        SimpleNamespace(source_chat_id=SOURCE_CHAT, source_message_id=1,
                        media_group_id="grp", topic_id=23, sender_id=ADMIN_ID,
                        media_type="photo", file_id="f", caption="EP 1165"),
        "One Piece", "\U0001F4FA",
    )
    clock.advance(60)
    results = await album_service.finalize_due()
    assert [r.album_id for r in results] == [album_id]
    assert (await db.get_album(album_id))["status"] == Status.PENDING


async def test_defect8_album_publishes_once_with_correct_media(album_service, db, clock,
                                                              auto_config, fake_bot):
    service = type(album_service)(db, album_service.publisher, fake_bot, auto_config, now_fn=clock)
    for message_id in (1, 2, 3):
        await service.ingest_part(
            SimpleNamespace(source_chat_id=SOURCE_CHAT, source_message_id=message_id,
                            media_group_id="grp2", topic_id=23, sender_id=ADMIN_ID,
                            media_type="photo", file_id=f"f{message_id}",
                            caption="EP 1165" if message_id == 1 else None),
            "One Piece", "\U0001F4FA",
        )
    clock.advance(60)
    await service.finalize_due()
    assert len(fake_bot.album_calls) == 1
    media = fake_bot.album_calls[0]["media"]
    assert len(media) == 3
    assert [m.media for m in media] == ["f1", "f2", "f3"]
    assert media[0].caption and media[1].caption is None


# --------------------------------------------------------------------------- #
# Defect 9 - event loop on Python 3.14
# --------------------------------------------------------------------------- #


def test_defect9_get_event_loop_raises_without_a_loop():
    probe = subprocess.run(
        [sys.executable, "-c",
         "import asyncio\n"
         "try:\n    asyncio.get_event_loop()\n    print('NO')\n"
         "except RuntimeError:\n    print('RAISES')\n"],
        capture_output=True, text=True, timeout=60,
    )
    assert "RAISES" in probe.stdout


def test_defect9_ptb_requires_a_current_loop():
    from telegram.ext import Application

    assert "asyncio.get_event_loop()" in inspect.getsource(Application._Application__run)


def test_defect9_main_installs_the_loop():
    root = Path(__file__).resolve().parent.parent
    source = (root / "bot.py").read_text(encoding="utf-8")
    main_source = source.split("def main(")[1]
    assert "asyncio.set_event_loop(asyncio.new_event_loop())" in main_source


# --------------------------------------------------------------------------- #
# Defect 10 - deprecated run_polling timeout arguments
# --------------------------------------------------------------------------- #


def test_defect10_no_deprecated_polling_arguments():
    root = Path(__file__).resolve().parent.parent
    source = (root / "bot.py").read_text(encoding="utf-8")
    main_source = source.split("def main(")[1]
    for deprecated in ("read_timeout", "write_timeout", "connect_timeout"):
        assert deprecated not in source, f"{deprecated} still present"
    assert "run_polling(" in main_source


def test_defect10_run_polling_signature_still_valid():
    """The arguments actually used must still be supported by this PTB version."""
    from telegram.ext import Application

    signature = inspect.signature(Application.run_polling)
    assert "drop_pending_updates" in signature.parameters
    assert "close_loop" in signature.parameters


# --------------------------------------------------------------------------- #
# Additional regression: callback actions must validate
# --------------------------------------------------------------------------- #


def test_single_letter_callback_actions_are_accepted():
    """A previous regex required two characters and silently rejected p/c/e."""
    for action, kind, row_id in (
        (ui.ACT_PUBLISH, ui.KIND_ITEM, 1),
        (ui.ACT_CANCEL, ui.KIND_ITEM, 1),
        (ui.ACT_EDIT, ui.KIND_ITEM, 1),
        (ui.ACT_UNCERTAIN_PUBLISHED, ui.KIND_ALBUM, 2),
    ):
        assert ui.decode_callback(ui.encode_callback(action, kind, row_id)) == (action, kind, row_id)


def test_unknown_callback_actions_are_rejected():
    assert ui.decode_callback("aw:zz:m:1") is None


async def test_publish_callback_actually_publishes(handlers, db, fake_bot):
    """End-to-end proof the single-letter publish action reaches Telegram."""
    _, row_id = await make_item(db)
    query = FakeQuery(ui.encode_callback(ui.ACT_PUBLISH, ui.KIND_ITEM, row_id))
    update = SimpleNamespace(callback_query=query, effective_user=query.from_user,
                             effective_chat=None, effective_message=None)
    await handlers.handle_callback(update, FakeContext())

    assert (await db.get_item(row_id))["status"] == Status.PUBLISHED
    assert len(fake_bot.copy_calls) == 1


async def test_cancel_callback_actually_cancels(handlers, db):
    _, row_id = await make_item(db)
    query = FakeQuery(ui.encode_callback(ui.ACT_CANCEL, ui.KIND_ITEM, row_id))
    update = SimpleNamespace(callback_query=query, effective_user=query.from_user,
                             effective_chat=None, effective_message=None)
    await handlers.handle_callback(update, FakeContext())
    assert (await db.get_item(row_id))["status"] == Status.CANCELED