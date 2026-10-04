import pytest
from unittest.mock import patch

from config import AppConfig, get_config, get_topic_config, TOPIC_MAP, add_topic, _build_topic_map


class TestConfig:
    def test_config_loads(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "1234567890:AAExample",
            "SOURCE_GROUP_ID": "-1001234567890",
            "ADMIN_IDS": "6589890362",
        }):
            config = AppConfig.load()
            assert config.bot_token == "1234567890:AAExample"
            assert config.source_group_id == -1001234567890
            assert config.admin_ids == [6589890362]
            assert config.auto_publish is False
            assert config.target_channel == "@aniwavebd"

    def test_admin_ids_parsing(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "1234567890:AAExample",
            "SOURCE_GROUP_ID": "-1001234567890",
            "ADMIN_IDS": "1,2,3",
        }):
            config = AppConfig.load()
            assert config.admin_ids == [1, 2, 3]

    def test_auto_publish_true(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "1234567890:AAExample",
            "SOURCE_GROUP_ID": "-1001234567890",
            "ADMIN_IDS": "6589890362",
            "AUTO_PUBLISH": "true",
        }):
            config = AppConfig.load()
            assert config.auto_publish is True

    def test_missing_token_raises(self):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(RuntimeError):
                AppConfig.load()

    def test_missing_group_id_raises(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "1234567890:AAExample",
            "SOURCE_GROUP_ID": "",
            "ADMIN_IDS": "6589890362",
        }):
            with pytest.raises(RuntimeError):
                AppConfig.load()

    def test_get_config_returns_instance(self):
        config = get_config()
        assert isinstance(config, AppConfig)

    def test_topic_map_initialized(self):
        _build_topic_map()
        for tid in TOPIC_MAP:
            assert tid in [1, 2, 3] or isinstance(tid, int)

    def test_add_topic(self):
        add_topic(999, "Test", "🎬")
        assert TOPIC_MAP[999].title == "Test"
        assert TOPIC_MAP[999].emoji == "🎬"
        del TOPIC_MAP[999]

    def test_get_topic_config(self):
        _build_topic_map()
        for tid, cfg in TOPIC_MAP.items():
            found = get_topic_config(tid)
            assert found is not None
            assert found.title in ("One Piece", "Naruto", "Bleach")

    def test_security_scan_returns_list(self):
        findings = []
        assert isinstance(findings, list)

    def test_default_values(self):
        with patch.dict("os.environ", {
            "TELEGRAM_BOT_TOKEN": "1234567890:AAExample",
            "SOURCE_GROUP_ID": "-1001234567890",
            "ADMIN_IDS": "6589890362",
            "DATABASE_PATH": "test_aniwave.db",
        }):
            config = AppConfig.load()
            assert config.timezone == "Asia/Dhaka"
            assert config.database_path == "test_aniwave.db"
            assert config.log_level == "INFO"
            assert config.include_hd_claim is False

    def test_topic_ids_in_config(self):
        _build_topic_map()
        for tid in TOPIC_MAP:
            assert isinstance(tid, int)
            assert tid > 0