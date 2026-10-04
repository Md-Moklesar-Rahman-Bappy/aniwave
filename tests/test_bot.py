"""Application lifecycle, error handling, logging and startup validation.

Phase A / L / P. No test here performs a network call: the bot object is always
replaced with :class:`~tests.conftest.FakeBot` before any lifecycle hook runs.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ApplicationBuilder

import bot as bot_module
from albums import AlbumService
from config import ConfigError
from database import Database, Status
from publisher import Publisher
from tests.conftest import ADMIN_ID, SOURCE_CHAT
from tests.conftest import fake_token
from tests.test_database import make_album, make_item


# --------------------------------------------------------------------------- #
# Shared database identity (Phase A.1-5 / N items 1-2)
# --------------------------------------------------------------------------- #


async def test_build_application_creates_exactly_one_database(env, tmp_path):
    values = dict(env)
    values["DATABASE_PATH"] = str(tmp_path / "shared.db")
    from config import AppConfig

    config = AppConfig.load(values)
    application = bot_module.build_application(config)

    database = application.bot_data["database"]
    publisher = application.bot_data["publisher"]
    albums = application.bot_data["albums"]
    handlers = application.bot_data["handlers"]

    assert database is publisher.db
    assert database is albums.db
    assert database is handlers.db
    assert database is handlers.publisher.db
    assert application.bot_data["albums"].publisher is publisher


async def test_build_application_registers_handlers_and_error_handler(env, tmp_path):
    from config import AppConfig

    values = dict(env)
    values["DATABASE_PATH"] = str(tmp_path / "h.db")
    application = bot_module.build_application(AppConfig.load(values))

    assert application.handlers
    assert len(application.error_handlers) == 1
    assert application.bot_data["config"] is not None


async def test_bot_data_holds_the_shared_instances(env, tmp_path):
    from config import AppConfig

    values = dict(env)
    values["DATABASE_PATH"] = str(tmp_path / "bd.db")
    application = bot_module.build_application(AppConfig.load(values))
    for key in ("database", "publisher", "albums", "handlers", "config"):
        assert key in application.bot_data


# --------------------------------------------------------------------------- #
# post_init / post_shutdown (Phase A.3-4 / P.6-7)
# --------------------------------------------------------------------------- #


def make_lifecycle_application(env, tmp_path, fake_bot):
    from config import AppConfig

    values = dict(env)
    values["DATABASE_PATH"] = str(tmp_path / "lifecycle.db")
    config = AppConfig.load(values)

    application = ApplicationBuilder().token(config.bot_token).build()
    application.bot_data["config"] = config
    database = Database(config.database_path)
    application.bot_data["database"] = database
    publisher = Publisher(fake_bot, database, config)
    albums = AlbumService(database, publisher, fake_bot, config)
    application.bot_data["publisher"] = publisher
    application.bot_data["albums"] = albums
    application.bot = fake_bot
    return application, database


async def test_post_init_connects_the_shared_database(env, tmp_path, fake_bot):
    application, database = make_lifecycle_application(env, tmp_path, fake_bot)
    assert database.is_connected is False

    await bot_module.post_init(application)
    try:
        assert database.is_connected is True
        assert database.schema_version > 0
    finally:
        await bot_module.post_shutdown(application)


async def test_post_shutdown_closes_the_shared_database(env, tmp_path, fake_bot):
    application, database = make_lifecycle_application(env, tmp_path, fake_bot)
    await bot_module.post_init(application)
    await bot_module.post_shutdown(application)
    assert database.is_connected is False


async def test_post_shutdown_is_safe_without_a_database(env, tmp_path, fake_bot):
    application, _ = make_lifecycle_application(env, tmp_path, fake_bot)
    application.bot_data.pop("database")
    application.bot_data.pop("albums")
    await bot_module.post_shutdown(application)


async def test_post_init_starts_the_album_reconciler(env, tmp_path, fake_bot):
    application, _ = make_lifecycle_application(env, tmp_path, fake_bot)
    await bot_module.post_init(application)
    try:
        albums = application.bot_data["albums"]
        assert albums._task is not None and not albums._task.done()
    finally:
        await bot_module.post_shutdown(application)


async def test_post_init_recovers_interrupted_records_and_notifies(env, tmp_path, fake_bot):
    application, database = make_lifecycle_application(env, tmp_path, fake_bot)
    await database.connect()
    _, row_id = await make_item(database)
    await database.claim_item_for_publishing(row_id)
    await database.close()

    await bot_module.post_init(application)
    try:
        rows = await database.uncertain_rows()
        assert [int(r["id"]) for r in rows] == [row_id]
        assert any("UNCERTAIN" in m["text"] for m in fake_bot.sent_messages)
        assert fake_bot.sent_messages[0]["chat_id"] == SOURCE_CHAT
    finally:
        await bot_module.post_shutdown(application)


async def test_post_init_survives_identity_check_failure(env, tmp_path, fake_bot):
    application, database = make_lifecycle_application(env, tmp_path, fake_bot)
    fake_bot.get_me = _raising_get_me
    await bot_module.post_init(application)
    try:
        assert database.is_connected is True
    finally:
        await bot_module.post_shutdown(application)


async def _raising_get_me():
    raise BadRequest("cannot reach telegram")


# --------------------------------------------------------------------------- #
# Error handler (Phase A.6-7 / N items 31)
# --------------------------------------------------------------------------- #


class FakeContextWithError:
    def __init__(self, error, application=None):
        self.error = error
        self.application = application


def make_update_stub(chat_id=SOURCE_CHAT, message_id=7, user_id=ADMIN_ID):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(message_id=message_id),
    )


async def test_error_handler_logs_without_update(env, tmp_path, fake_bot, caplog):
    """A None update must not raise."""
    application, _ = make_lifecycle_application(env, tmp_path, fake_bot)
    context = FakeContextWithError(ValueError("boom"), application)
    with caplog.at_level(logging.ERROR):
        await bot_module.error_handler(None, context)
    assert any("unhandled-error" in record.message for record in caplog.records)


async def test_error_handler_logs_with_context(env, tmp_path, fake_bot, caplog):
    application, _ = make_lifecycle_application(env, tmp_path, fake_bot)
    context = FakeContextWithError(ValueError("boom"), application)
    with caplog.at_level(logging.ERROR):
        await bot_module.error_handler(make_update_stub(), context)
    text = caplog.text
    assert str(SOURCE_CHAT) in text
    assert "ValueError" in text


async def test_error_handler_tolerates_missing_error(env, tmp_path, fake_bot):
    application, _ = make_lifecycle_application(env, tmp_path, fake_bot)
    context = FakeContextWithError(None, application)
    await bot_module.error_handler(make_update_stub(), context)


async def test_error_handler_notifies_admin_without_secrets(env, tmp_path, fake_bot):
    application, _ = make_lifecycle_application(env, tmp_path, fake_bot)
    context = FakeContextWithError(RuntimeError("kaboom"), application)
    await bot_module.error_handler(make_update_stub(), context)

    assert fake_bot.sent_messages
    notice = fake_bot.sent_messages[0]
    assert notice["chat_id"] == SOURCE_CHAT
    text = notice["text"]
    assert "RuntimeError" in text
    for secret in application.bot_data["config"].secrets():
        assert secret not in text
    assert "Traceback" not in text


async def test_error_handler_rate_limits_notifications(env, tmp_path, fake_bot):
    application, _ = make_lifecycle_application(env, tmp_path, fake_bot)
    context = FakeContextWithError(RuntimeError("kaboom"), application)
    for _ in range(5):
        await bot_module.error_handler(make_update_stub(), context)
    assert len(fake_bot.sent_messages) == 1


async def test_error_handler_survives_notification_failure(env, tmp_path, fake_bot):
    application, _ = make_lifecycle_application(env, tmp_path, fake_bot)
    fake_bot.send_error = RuntimeError("network down")
    context = FakeContextWithError(RuntimeError("kaboom"), application)
    await bot_module.error_handler(make_update_stub(), context)


async def test_error_handler_survives_missing_application():
    context = FakeContextWithError(RuntimeError("kaboom"), None)
    await bot_module.error_handler(make_update_stub(), context)


# --------------------------------------------------------------------------- #
# Logging / redaction (Phase L)
# --------------------------------------------------------------------------- #


def test_redaction_filter_scrubs_message_and_args():
    secret = fake_token("s")
    log_filter = bot_module.SecretRedactionFilter((secret,))
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="using %s now", args=(secret,), exc_info=None,
    )
    assert log_filter.filter(record) is True
    rendered = record.getMessage()
    assert secret not in rendered
    assert "REDACTED" in rendered


def test_redaction_filter_scrubs_exception_text():
    secret = fake_token("s")
    log_filter = bot_module.SecretRedactionFilter((secret,))
    caught = None
    try:
        raise ValueError(f"failed with {secret}")
    except ValueError as exc:
        caught = exc
    record = logging.LogRecord(
        name="t", level=logging.ERROR, pathname=__file__, lineno=1,
        msg="oops", args=None, exc_info=(type(caught), caught, caught.__traceback__),
    )
    log_filter.filter(record)
    assert secret not in str(caught.args)


def test_redaction_filter_handles_dict_args():
    log_filter = bot_module.SecretRedactionFilter()
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="%(token)s", args=None, exc_info=None,
    )
    # LogRecord cannot be constructed with a dict positional arg on 3.14, so it
    # is assigned afterwards.
    record.args = {"token": fake_token("t")}
    log_filter.filter(record)
    assert "REDACTED" in record.getMessage()


def test_configure_logging_is_idempotent():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        bot_module.configure_logging("INFO", ("s3cret",))
        baseline = len(root.handlers)
        for _ in range(3):
            bot_module.configure_logging("INFO", ("s3cret",))
        assert len(root.handlers) == baseline
    finally:
        for handler in list(root.handlers):
            if handler not in saved_handlers:
                root.removeHandler(handler)
                handler.close()
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def test_configure_logging_replaces_stale_redaction_filter():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    try:
        bot_module.configure_logging("INFO", ("first",))
        bot_module.configure_logging("INFO", ("second",))
        for handler in root.handlers:
            redaction = [f for f in handler.filters
                         if isinstance(f, bot_module.SecretRedactionFilter)]
            assert len(redaction) <= 1, "redaction filter must not be duplicated"
    finally:
        for handler in list(root.handlers):
            if handler not in saved_handlers:
                root.removeHandler(handler)
                handler.close()
        root.handlers = saved_handlers


def test_configure_logging_redacts_registered_secret(caplog):
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    secret = fake_token("l")
    try:
        bot_module.configure_logging("INFO", (secret,))
        logger = logging.getLogger("test-redaction")
        with caplog.at_level(logging.INFO):
            logger.info("token=%s", secret)
        assert secret not in caplog.text
    finally:
        for handler in list(root.handlers):
            if handler not in saved_handlers:
                root.removeHandler(handler)
                handler.close()
        root.handlers = saved_handlers


async def test_database_logs_only_the_file_name(env, tmp_path, caplog):
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    try:
        bot_module.configure_logging("DEBUG", ())
        path = tmp_path / "named-only.db"
        with caplog.at_level(logging.DEBUG, logger="database"):
            database = Database(str(path))
            await database.connect()
            await database.close()
        assert "named-only.db" in caplog.text
        assert str(tmp_path) not in caplog.text
    finally:
        for handler in list(root.handlers):
            if handler not in saved_handlers:
                root.removeHandler(handler)
                handler.close()
        root.handlers = saved_handlers


# --------------------------------------------------------------------------- #
# Event loop / run_polling arguments (Phase A.9-11)
# --------------------------------------------------------------------------- #


def _run_probe(source: str) -> subprocess.CompletedProcess:
    """Run a probe in a clean interpreter.

    Event-loop state is global, so the checks below must not run inside the
    test process where pytest-asyncio already owns a loop.
    """
    return subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_get_event_loop_raises_without_a_loop_on_this_interpreter():
    """This is why main() must install a loop before run_polling()."""
    probe = _run_probe(
        "import asyncio\n"
        "try:\n"
        "    asyncio.get_event_loop()\n"
        "    print('NO-RAISE')\n"
        "except RuntimeError:\n"
        "    print('RAISES')\n"
    )
    assert "RAISES" in probe.stdout


def test_get_event_loop_works_once_a_loop_is_installed():
    probe = _run_probe(
        "import asyncio\n"
        "asyncio.set_event_loop(asyncio.new_event_loop())\n"
        "print(type(asyncio.get_event_loop()).__name__)\n"
    )
    assert "ProactorEventLoop" in probe.stdout or "SelectorEventLoop" in probe.stdout


def test_ptb_calls_get_event_loop_during_run_polling():
    """Confirms the dependency on an installed loop is real, not cargo cult."""
    import inspect

    from telegram.ext import Application

    source = inspect.getsource(Application._Application__run)
    assert "asyncio.get_event_loop()" in source


def test_main_installs_an_event_loop():
    source = Path(bot_module.__file__).read_text(encoding="utf-8")
    assert "asyncio.set_event_loop(asyncio.new_event_loop())" in source


def test_no_nested_asyncio_run_around_run_polling():
    source = Path(bot_module.__file__).read_text(encoding="utf-8")
    main_source = source.split("def main(")[1]
    assert "run_polling" in main_source
    assert "asyncio.run(" not in main_source


def test_run_polling_has_no_deprecated_timeout_arguments():
    source = Path(bot_module.__file__).read_text(encoding="utf-8")
    main_source = source.split("def main(")[1]
    for deprecated in ("read_timeout", "write_timeout", "connect_timeout", "timeout="):
        assert deprecated not in main_source


# --------------------------------------------------------------------------- #
# Startup validation / preflight (Phase B.13, security)
# --------------------------------------------------------------------------- #


def test_main_reports_invalid_configuration_without_a_token(monkeypatch, capsys):
    for key in (
        "TELEGRAM_BOT_TOKEN", "SOURCE_GROUP_ID", "TARGET_CHANNEL", "ADMIN_IDS",
        "WEB_SERIES_TOPIC_ID", "AUTO_PUBLISH",
        "INCLUDE_HD_CLAIM", "TIMEZONE", "DATABASE_PATH", "LOG_LEVEL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AUTO_PUBLISH", "maybe")

    exit_code = bot_module.main()
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Configuration error" in captured.err
    assert "TELEGRAM_BOT_TOKEN" in captured.err
    assert ".env" in captured.err


def test_main_config_error_never_prints_a_token(monkeypatch, capsys):
    env_token = fake_token("m")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", env_token)
    monkeypatch.setenv("SOURCE_GROUP_ID", "not-a-number")
    monkeypatch.setenv("TARGET_CHANNEL", "@channel")
    monkeypatch.setenv("ADMIN_IDS", "1")
    monkeypatch.setenv("WEB_SERIES_TOPIC_ID", "33")
    monkeypatch.setenv("AUTO_PUBLISH", "false")
    monkeypatch.setenv("INCLUDE_HD_CLAIM", "false")
    monkeypatch.setenv("TIMEZONE", "Asia/Dhaka")
    monkeypatch.setenv("DATABASE_PATH", "x.db")
    monkeypatch.setenv("LOG_LEVEL", "INFO")

    bot_module.main()
    captured = capsys.readouterr()
    assert env_token not in captured.err


def test_preflight_detects_seeded_secret(tmp_path, monkeypatch):
    secret = fake_token("p")
    (tmp_path / "leak.py").write_text(f'X = "{secret}"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    from config import AppConfig

    config = AppConfig.load({
        "TELEGRAM_BOT_TOKEN": "1:AAx",
        "SOURCE_GROUP_ID": "-1001", "TARGET_CHANNEL": "@channel", "ADMIN_IDS": "1",
        "WEB_SERIES_TOPIC_ID": "33",
        "AUTO_PUBLISH": "false", "INCLUDE_HD_CLAIM": "false", "TIMEZONE": "Asia/Dhaka",
        "DATABASE_PATH": "d.db", "LOG_LEVEL": "INFO",
    })
    with pytest.raises(ConfigError) as excinfo:
        bot_module.preflight(config)
    message = str(excinfo.value)
    assert "leak.py" in message
    assert secret not in message


def test_preflight_passes_for_a_clean_tree(env, tmp_path, monkeypatch):
    (tmp_path / "ok.py").write_text("X = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    from config import AppConfig

    config = AppConfig.load(env)
    bot_module.preflight(config)  # must not raise