import asyncio
import logging

from telegram.ext import ApplicationBuilder

from config import get_config
from database import Database
from publisher import Publisher
from handlers import Handlers


logger = logging.getLogger(__name__)


def setup_logging(log_level: str = "INFO") -> None:
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=getattr(logging, log_level.upper(), logging.INFO),
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def post_init(application) -> None:
    db = application.bot_data["database"]
    await db.connect()
    await db.cleanup_stale_publishing()
    bot_info = await application.bot.get_me()
    logger.info("Database connected and stale publishing items recovered.")
    logger.info("Telegram bot connected: @%s", bot_info.username)


async def post_shutdown(application) -> None:
    db = application.bot_data.get("database")
    if db is not None:
        await db.close()
        logger.info("Database connection closed.")


async def error_handler(update, context) -> None:
    logger.exception(
        "Unhandled Telegram update error: %s",
        context.error,
        exc_info=context.error,
    )


def build_application():
    config = get_config()
    setup_logging(config.log_level)

    database = Database()
    application = (
        ApplicationBuilder()
        .token(config.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.bot_data["database"] = database

    publisher = Publisher(application.bot, database)
    handlers = Handlers(database, publisher)

    for handler in handlers.get_handlers():
        application.add_handler(handler)

    application.add_error_handler(error_handler)

    return application


def main() -> None:
    asyncio.set_event_loop(asyncio.new_event_loop())

    config = get_config()
    setup_logging(config.log_level)

    security_findings = config.security_scan()
    if security_findings:
        logger.error(
            "Security check FAILED. Possible Telegram bot tokens found.",
        )
        raise SystemExit(1)

    application = build_application()
    logger.info("Starting AniWave Bot...")
    application.run_polling(
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
