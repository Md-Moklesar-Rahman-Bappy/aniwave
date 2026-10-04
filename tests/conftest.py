import os
import pytest
import tempfile

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1234567890:AAExampleToken")
os.environ.setdefault("SOURCE_GROUP_ID", "-1001234567890")
os.environ.setdefault("ADMIN_IDS", "6589890362")
os.environ.setdefault("ONE_PIECE_TOPIC_ID", "1")
os.environ.setdefault("NARUTO_TOPIC_ID", "2")
os.environ.setdefault("BLEACH_TOPIC_ID", "3")
os.environ.setdefault("TARGET_CHANNEL", "@aniwavebd")
os.environ.setdefault("AUTO_PUBLISH", "false")
os.environ.setdefault("INCLUDE_HD_CLAIM", "false")
os.environ.setdefault("TIMEZONE", "Asia/Dhaka")
os.environ.setdefault("DATABASE_PATH", "test_aniwave.db")
os.environ.setdefault("LOG_LEVEL", "INFO")


@pytest.fixture
async def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from database import Database
    db = Database()
    db._db_path = path
    await db.connect()
    yield db
    await db.close()
    os.unlink(path)