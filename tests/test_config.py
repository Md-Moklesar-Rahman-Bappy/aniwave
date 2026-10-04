"""Configuration parsing, validation and secret-safety tests (Phase B)."""

from __future__ import annotations

import pytest

from config import (
    DEFAULT_ALBUM_QUIET_SECONDS,
    LOG_LEVELS,
    ConfigError,
    REDACTED,
    TOKEN_PATTERN,
    AppConfig,
    build_topics,
    load_config,
    redact,
    reset_config_cache,
    scan_for_secrets,
)
from tests.conftest import PLACEHOLDER_TOKEN


# --------------------------------------------------------------------------- #
# Successful parsing
# --------------------------------------------------------------------------- #


def test_loads_valid_environment(env):
    config = AppConfig.load(env)
    assert config.source_group_id == -1004428338491
    assert config.admin_ids == (6589890362,)
    assert config.auto_publish is False
    assert config.include_hd_claim is False
    assert config.log_level == "INFO"
    assert config.timezone == "Asia/Dhaka"


def test_target_channel_stays_a_string(env):
    config = AppConfig.load(env)
    assert config.target_channel == "@aniwavebd"
    assert isinstance(config.target_channel, str)


def test_numeric_target_channel_stays_string(env):
    env["TARGET_CHANNEL"] = "-1009876543210"
    config = AppConfig.load(env)
    assert config.target_channel == "-1009876543210"
    assert isinstance(config.target_channel, str)


def test_admin_ids_comma_separated_with_duplicates(env):
    env["ADMIN_IDS"] = " 111 , 222 ,111 , 333 "
    config = AppConfig.load(env)
    assert config.admin_ids == (111, 222, 333)


def test_values_are_trimmed(env):
    env["SOURCE_GROUP_ID"] = "  -1004428338491  "
    env["TIMEZONE"] = "  Asia/Dhaka "
    config = AppConfig.load(env)
    assert config.source_group_id == -1004428338491
    assert config.timezone == "Asia/Dhaka"


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("TRUE", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("FALSE", False), ("0", False), ("no", False), ("off", False),
])
def test_boolean_spellings(env, raw, expected):
    env["AUTO_PUBLISH"] = raw
    assert AppConfig.load(env).auto_publish is expected


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("0", False), ("Yes", True), ("off", False),
])
def test_include_hd_claim_spellings(env, raw, expected):
    env["INCLUDE_HD_CLAIM"] = raw
    assert AppConfig.load(env).include_hd_claim is expected


@pytest.mark.parametrize("level", LOG_LEVELS)
def test_valid_log_levels(env, level):
    env["LOG_LEVEL"] = level.lower()
    assert AppConfig.load(env).log_level == level


def test_default_album_quiet_seconds(env):
    assert AppConfig.load(env).album_quiet_seconds == DEFAULT_ALBUM_QUIET_SECONDS


@pytest.mark.parametrize("raw,expected", [("1", 1), ("10", 10), ("120", 120)])
def test_album_quiet_seconds_parsed(env, raw, expected):
    env["ALBUM_QUIET_SECONDS"] = raw
    assert AppConfig.load(env).album_quiet_seconds == expected


# --------------------------------------------------------------------------- #
# Topics
# --------------------------------------------------------------------------- #


def test_topics_built_from_environment(env):
    config = AppConfig.load(env)
    assert config.topic_ids() == [6, 8, 23]
    assert config.topic_for(23).title == "One Piece"
    assert config.topic_for(6).title == "Naruto"
    assert config.topic_for(8).title == "Bleach"


def test_topic_for_unknown_topic_returns_none(env):
    config = AppConfig.load(env)
    assert config.topic_for(999) is None
    assert config.topic_for(None) is None


def test_build_topics_is_fresh_each_call(env):
    first = build_topics(env)
    env["ONE_PIECE_TOPIC_ID"] = "99"
    second = build_topics(env)
    assert first != second
    assert [t.topic_id for t in first if t.title == "One Piece"] == [23]
    assert [t.topic_id for t in second if t.title == "One Piece"] == [99]


def test_duplicate_topic_ids_rejected(env):
    env["BLEACH_TOPIC_ID"] = env["ONE_PIECE_TOPIC_ID"]
    with pytest.raises(ConfigError) as excinfo:
        AppConfig.load(env)
    assert "unique" in str(excinfo.value)


def test_is_admin_uses_numeric_ids(env):
    config = AppConfig.load(env)
    assert config.is_admin(6589890362) is True
    assert config.is_admin(1) is False
    assert config.is_admin(None) is False


# --------------------------------------------------------------------------- #
# Failure paths
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("missing", [
    "TELEGRAM_BOT_TOKEN", "SOURCE_GROUP_ID", "TARGET_CHANNEL", "ADMIN_IDS",
    "ONE_PIECE_TOPIC_ID", "NARUTO_TOPIC_ID", "BLEACH_TOPIC_ID", "AUTO_PUBLISH",
    "INCLUDE_HD_CLAIM", "TIMEZONE", "DATABASE_PATH", "LOG_LEVEL",
])
def test_missing_required_variable_is_reported(env, missing):
    env.pop(missing)
    with pytest.raises(ConfigError) as excinfo:
        AppConfig.load(env)
    assert missing in str(excinfo.value)


def test_blank_required_variable_is_rejected(env):
    env["SOURCE_GROUP_ID"] = "   "
    with pytest.raises(ConfigError) as excinfo:
        AppConfig.load(env)
    assert "SOURCE_GROUP_ID" in str(excinfo.value)


def test_missing_token_message_explains_local_setup(env):
    env.pop("TELEGRAM_BOT_TOKEN")
    with pytest.raises(ConfigError) as excinfo:
        AppConfig.load(env)
    message = str(excinfo.value)
    assert ".env" in message
    assert "Copy .env.example" in message


def test_empty_admin_list_rejected(env):
    env["ADMIN_IDS"] = " , , "
    with pytest.raises(ConfigError) as excinfo:
        AppConfig.load(env)
    assert "ADMIN_IDS" in str(excinfo.value)


def test_non_numeric_admin_rejected(env):
    env["ADMIN_IDS"] = "123,notanumber"
    with pytest.raises(ConfigError) as excinfo:
        AppConfig.load(env)
    assert "non-numeric" in str(excinfo.value)


def test_negative_admin_rejected(env):
    env["ADMIN_IDS"] = "-5"
    with pytest.raises(ConfigError):
        AppConfig.load(env)


def test_invalid_boolean_rejected(env):
    env["AUTO_PUBLISH"] = "maybe"
    with pytest.raises(ConfigError) as excinfo:
        AppConfig.load(env)
    assert "true/false" in str(excinfo.value)


def test_invalid_log_level_rejected(env):
    env["LOG_LEVEL"] = "VERBOSE"
    with pytest.raises(ConfigError) as excinfo:
        AppConfig.load(env)
    assert "LOG_LEVEL" in str(excinfo.value)


def test_invalid_timezone_rejected(env):
    env["TIMEZONE"] = "Mars/Olympus_Mons"
    with pytest.raises(ConfigError) as excinfo:
        AppConfig.load(env)
    assert "timezone" in str(excinfo.value).lower()


def test_non_numeric_source_group_rejected(env):
    env["SOURCE_GROUP_ID"] = "mygroup"
    with pytest.raises(ConfigError) as excinfo:
        AppConfig.load(env)
    assert "SOURCE_GROUP_ID" in str(excinfo.value)


def test_zero_source_group_rejected(env):
    env["SOURCE_GROUP_ID"] = "0"
    with pytest.raises(ConfigError):
        AppConfig.load(env)


def test_topic_id_must_be_positive_integer(env):
    env["NARUTO_TOPIC_ID"] = "-6"
    with pytest.raises(ConfigError) as excinfo:
        AppConfig.load(env)
    assert "NARUTO_TOPIC_ID" in str(excinfo.value)


def test_topic_id_must_be_numeric(env):
    env["NARUTO_TOPIC_ID"] = "abc"
    with pytest.raises(ConfigError):
        AppConfig.load(env)


def test_target_channel_without_at_or_digits_rejected(env):
    env["TARGET_CHANNEL"] = "aniwavebd"
    with pytest.raises(ConfigError) as excinfo:
        AppConfig.load(env)
    assert "TARGET_CHANNEL" in str(excinfo.value)


def test_album_quiet_seconds_out_of_range(env):
    env["ALBUM_QUIET_SECONDS"] = "9999"
    with pytest.raises(ConfigError) as excinfo:
        AppConfig.load(env)
    assert "ALBUM_QUIET_SECONDS" in str(excinfo.value)


def test_all_problems_reported_together(env):
    env["AUTO_PUBLISH"] = "maybe"
    env["LOG_LEVEL"] = "NOPE"
    with pytest.raises(ConfigError) as excinfo:
        AppConfig.load(env)
    message = str(excinfo.value)
    assert "AUTO_PUBLISH" in message and "LOG_LEVEL" in message


# --------------------------------------------------------------------------- #
# Secret safety
# --------------------------------------------------------------------------- #


def test_error_messages_never_contain_the_token(env):
    env["AUTO_PUBLISH"] = "maybe"
    with pytest.raises(ConfigError) as excinfo:
        AppConfig.load(env)
    assert PLACEHOLDER_TOKEN not in str(excinfo.value)


def test_redact_removes_token_shaped_strings():
    text = "leaking 123456789:AAabcdefghijklmnopqrstuvwxyz012345 here"
    cleaned = redact(text)
    assert "123456789:AA" not in cleaned
    assert REDACTED in cleaned


def test_redact_removes_known_secret():
    secret = "999888777:ZZsecretvaluehere_abcdefghijklmnop"
    cleaned = redact(f"token is {secret} ok", secrets=(secret,))
    assert secret not in cleaned


def test_redact_leaves_safe_text_untouched():
    assert redact("published item 12 to chat -100123") == "published item 12 to chat -100123"


def test_token_pattern_matches_expected_shape():
    assert TOKEN_PATTERN.search("1234567890:AA" + "b" * 35)
    assert not TOKEN_PATTERN.search("12345:short")


def test_scan_finds_secret_locations(tmp_path):
    target = tmp_path / "leaky.txt"
    target.write_text("value = 123456789:AA" + "c" * 35 + "\n", encoding="utf-8")
    findings = scan_for_secrets(str(tmp_path))
    assert any("leaky.txt:1" in f for f in findings)


def test_scan_ignores_env_file(tmp_path):
    (tmp_path / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=123456789:AA" + "d" * 35 + "\n", encoding="utf-8"
    )
    assert scan_for_secrets(str(tmp_path)) == []


def test_scan_reports_location_not_secret(tmp_path):
    secret = "123456789:AA" + "e" * 35
    (tmp_path / "leaky.py").write_text(f'TOKEN = "{secret}"\n', encoding="utf-8")
    findings = scan_for_secrets(str(tmp_path))
    assert findings
    assert all(secret not in finding for finding in findings)


def test_config_cache_roundtrip(env):
    reset_config_cache()
    config = load_config(env)
    assert isinstance(config, AppConfig)
    reset_config_cache()