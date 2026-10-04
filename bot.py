"""Application entry point: wiring, lifecycle, and error handling.

Lifecycle
---------
Exactly **one** :class:`~database.Database` instance is created in
:func:`build_application`, stored in ``application.bot_data``, connected in
``post_init`` and closed in ``post_shutdown``. The same instance is handed to
the publisher, the album service and the handlers - no component opens its own
connection.

Event loop
----------
python-telegram-bot 21.11.1 calls :func:`asyncio.get_event_loop` directly inside
``Application.__run``. On Python 3.14 that call *raises* ``RuntimeError`` when
no loop is set, so :func:`main` installs one explicitly. This was verified on
this interpreter rather than assumed.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any, Optional, Tuple

from telegram.error import TelegramError
from telegram.ext import Application, ApplicationBuilder

from albums import AlbumService
from config import (
    APP_VERSION,
    AppConfig,
    ConfigError,
    load_dotenv_file,
    load_config,
    redact,
    scan_for_secrets,
)
from database import Database
from handlers import Handlers
from publisher import Publisher

logger = logging.getLogger(__name__)

#: Minimum seconds between admin-facing error notices.
ERROR_NOTICE_INTERVAL = 60.0

_LOGGING_CONFIGURED = "_aniwave_logging_configured"


class SecretRedactionFilter(logging.Filter):
    """Scrub credentials from every log record.

    Both literal known secrets and anything shaped like a bot token are
    replaced, so even an unexpected leak cannot reach a log file or console.
    """

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg, self._secrets)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: (redact(value, self._secrets) if isinstance(value, str) else value)
                    for key, value in record.args.items()
                }
            else:
                record.args = tuple(
                    redact(value, self._secrets) if isinstance(value, str) else value
                    for value in record.args
                )
        if record.exc_info and record.exc_info[1] is not None:
            # Exception text is rendered into the traceback by the formatter;
            # pre-scrub the message so nothing sensitive is written out.
            exc = record.exc_info[1]
            try:
                exc.args = tuple(redact(a, self._secrets) for a in exc.args)
            except Exception:  # noqa: BLE001 - never break logging
                pass
        return True


def _force_utf8_streams() -> None:
    """Make console output emoji-safe on Windows (default cp1252)."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - detached stream
            pass


def configure_logging(level: str, secrets: tuple[str, ...] = ()) -> None:
    """Idempotent logging setup - repeated calls never add duplicate handlers."""
    root = logging.getLogger()
    if getattr(root, _LOGGING_CONFIGURED, False):
        for handler in root.handlers:
            handler.filters = [
                f for f in handler.filters if not isinstance(f, SecretRedactionFilter)
            ]
            handler.addFilter(SecretRedactionFilter(secrets))
        root.setLevel(getattr(logging, level.upper(), logging.INFO))
        return

    _force_utf8_streams()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    )
    handler.addFilter(SecretRedactionFilter(secrets))
    root.addHandler(handler)

    # python-telegram-bot logs every API call at DEBUG, which can echo payloads.
    logging.getLogger("telegram").setLevel(max(logging.INFO, root.level))
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.Updater").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    setattr(root, _LOGGING_CONFIGURED, True)


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


async def post_init(application: Application) -> None:
    """Connect shared state, recover interrupted work, start background jobs."""
    database: Database = application.bot_data["database"]
    await database.connect()

    try:
        bot_info = await application.bot.get_me()
        application.bot_data["bot_username"] = getattr(bot_info, "username", None)
        logger.info("bot-connected username=%s", getattr(bot_info, "username", None))
    except TelegramError:
        logger.exception("bot-identity-check-failed")

    recovered = await database.recover_interrupted_publishing()
    total_uncertain = len(recovered["media_items"]) + len(recovered["albums"])
    if total_uncertain:
        logger.warning(
            "startup-recovery uncertain_records=%d action=manual_review_required", total_uncertain
        )
        await _notify_uncertain(application, recovered)
    else:
        logger.info("startup-recovery nothing_to_review")

    albums: AlbumService = application.bot_data["albums"]
    results = await albums.recover_on_startup()
    logger.info("startup-album-recovery finalized=%d", len(results))

    albums.start(application)
    config: AppConfig = application.bot_data["config"]
    logger.info(
        "startup-complete version=%s mode=%s db=%s",
        APP_VERSION,
        "automatic" if config.auto_publish else "approval",
        database._safe_path(),
    )


async def post_shutdown(application: Application) -> None:
    """Stop background jobs and close the shared database."""
    albums: Optional[AlbumService] = application.bot_data.get("albums")
    if albums is not None:
        await albums.stop()
    database: Optional[Database] = application.bot_data.get("database")
    if database is not None:
        await database.close()
    logger.info("shutdown-complete")


async def _notify_uncertain(application: Application, recovered: dict) -> None:
    """Tell admins which records need a manual channel check."""
    config: AppConfig = application.bot_data["config"]
    lines = ["Interrupted publications moved to UNCERTAIN - please check the channel:", ""]
    for row in recovered.get("media_items", []):
        lines.append(
            f"item {row['id']} - {row.get('anime_title') or 'unknown'} "
            f"episode {row.get('episode_number') or '?'} (message {row['source_message_id']})"
        )
    for row in recovered.get("albums", []):
        lines.append(
            f"album {row['id']} - {row.get('anime_title') or 'unknown'} "
            f"episode {row.get('episode_number') or '?'} "
            f"(group {row.get('media_group_id')}, {row.get('item_count', '?')} parts)"
        )
    lines += ["", "These will NOT be retried automatically. Use /uncertain to resolve them."]
    try:
        await application.bot.send_message(chat_id=config.source_group_id, text="\n".join(lines))
    except Exception:  # noqa: BLE001 - never block startup on a failed notice
        logger.exception("uncertain-notification-failed")


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #


def _describe_update(update: object) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """Extract (chat_id, message_id, user_id) without assuming a concrete type.

    Duck typing keeps diagnostics useful even when the update is partial.
    """
    if update is None:
        return None, None, None
    chat = getattr(update, "effective_chat", None)
    user = getattr(update, "effective_user", None)
    message = getattr(update, "effective_message", None)
    if chat is None and user is None and message is None:
        # Callback queries carry their sender on the query itself.
        query = getattr(update, "callback_query", None)
        if query is not None:
            query_user = getattr(query, "from_user", None)
            message = getattr(query, "message", None)
            chat = getattr(message, "chat", None)
            return (
                getattr(chat, "id", None),
                getattr(message, "message_id", None),
                getattr(query_user, "id", None),
            )
    return (
        getattr(chat, "id", None),
        getattr(message, "message_id", None),
        getattr(user, "id", None),
    )


async def error_handler(update: object, context: Any) -> None:
    """Log unexpected failures with safe context and notify admins sparingly."""
    error = getattr(context, "error", None)
    update_type = type(update).__name__ if update is not None else "None"
    chat_id, message_id, user_id = _describe_update(update)

    logger.error(
        "unhandled-error update=%s chat=%s message=%s user=%s error_type=%s",
        update_type, chat_id, message_id, user_id, type(error).__name__ if error else "None",
        exc_info=error,
    )

    if error is None:
        return

    application: Optional[Application] = getattr(context, "application", None)
    if application is None:
        return
    config: Optional[AppConfig] = application.bot_data.get("config")
    if config is None:
        return

    now = time.monotonic()
    last = application.bot_data.get("last_error_notice_at", 0.0)
    if now - last < ERROR_NOTICE_INTERVAL:
        return
    application.bot_data["last_error_notice_at"] = now

    try:
        await application.bot.send_message(
            chat_id=config.source_group_id,
            text=(
                "An internal error occurred and was logged.\n"
                f"type: {type(error).__name__}\n"
                "No secret values are included in this message."
            ),
        )
    except Exception:  # noqa: BLE001 - the error handler must never raise
        logger.debug("error-notification-failed", exc_info=True)


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def build_application(config: Optional[AppConfig] = None) -> Application:
    """Construct the application with a single shared database instance."""
    config = config or load_config()
    configure_logging(config.log_level, config.secrets())

    database = Database(config.database_path)
    application = (
        ApplicationBuilder()
        .token(config.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["config"] = config

    publisher = Publisher(application.bot, database, config)
    albums = AlbumService(database, publisher, application.bot, config)
    handlers = Handlers(database, publisher, albums, config)

    # bot_data is populated before post_init runs (post_init fires from
    # run_polling), so the hooks always see these shared instances.
    application.bot_data["database"] = database
    application.bot_data["publisher"] = publisher
    application.bot_data["albums"] = albums
    application.bot_data["handlers"] = handlers

    for handler in handlers.get_handlers():
        application.add_handler(handler)

    application.add_error_handler(error_handler)
    return application


def preflight(config: AppConfig) -> None:
    """Fail fast on configuration problems and leaked secrets.

    Only file locations are ever reported, never secret values.
    """
    findings = scan_for_secrets(".")
    if findings:
        logger.error(
            "security-check-failed token_like_values=%d locations=%s",
            len(findings),
            ", ".join(findings[:10]),
        )
        raise ConfigError(
            "A token-like value was found in the working tree. "
            "Remove it, keep only placeholders in .env.example, and rotate the bot "
            "token through BotFather. Locations: " + ", ".join(findings[:10])
        )


def main() -> int:
    load_dotenv_file()
    try:
        config = load_config(force=True)
    except ConfigError as exc:
        # No secret values appear in this message.
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        print(
            "\nSet TELEGRAM_BOT_TOKEN locally in .env (never commit it). "
            "Copy .env.example to .env to get started.",
            file=sys.stderr,
        )
        return 2

    configure_logging(config.log_level, config.secrets())
    logger.info(
        "starting version=%s mode=%s source_group=%s target=%s admins=%d topics=%s",
        APP_VERSION,
        "automatic" if config.auto_publish else "approval",
        config.source_group_id,
        config.target_channel,
        len(config.admin_ids),
        config.topic_ids(),
    )

    try:
        preflight(config)
    except ConfigError as exc:
        logger.error("preflight-failed %s", exc)
        return 3

    application = build_application(config)

    # Required on Python 3.14: PTB 21.11.1 calls asyncio.get_event_loop() itself,
    # which raises RuntimeError when no loop is installed for this thread.
    asyncio.set_event_loop(asyncio.new_event_loop())

    try:
        application.run_polling(
            drop_pending_updates=False,
            close_loop=True,
        )
    except KeyboardInterrupt:
        logger.info("interrupted-by-user")
    except TelegramError as exc:
        logger.error("telegram-error %s", type(exc).__name__)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())