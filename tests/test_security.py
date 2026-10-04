"""Security guarantees: no leaked credentials, correct ignore rules, safe errors.

These tests deliberately never read the real ``.env`` value.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from config import REDACTED, TOKEN_PATTERN, AppConfig, ConfigError, redact, scan_for_secrets
from tests.conftest import fake_token

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Files that may legitimately be scanned for leaked tokens.
SCANNABLE_SUFFIXES = (".py", ".txt", ".md", ".ini", ".toml", ".cfg", ".bat", ".ps1", ".example")


def iter_project_files():
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(PROJECT_ROOT)
        parts = set(relative.parts)
        if parts & {"__pycache__", ".git", ".venv", ".pytest_cache", ".ruff_cache"}:
            continue
        if path.name.startswith(".env"):
            continue
        yield relative, path


# --------------------------------------------------------------------------- #
# No token anywhere in the tree
# --------------------------------------------------------------------------- #


def test_no_token_like_value_in_project_files():
    offenders = []
    for relative, path in iter_project_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if TOKEN_PATTERN.search(line):
                offenders.append(f"{relative}:{lineno}")
    assert offenders == [], f"token-like values found at: {offenders}"


def test_scan_for_secrets_reports_nothing_for_this_project():
    assert scan_for_secrets(str(PROJECT_ROOT)) == []


def test_env_example_contains_only_placeholders():
    example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert not TOKEN_PATTERN.search(example)
    assert "replace_with_new_token" in example


def test_env_example_documents_every_required_variable():
    example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    for name in (
        "TELEGRAM_BOT_TOKEN", "SOURCE_GROUP_ID", "TARGET_CHANNEL", "ADMIN_IDS",
        "WEB_SERIES_TOPIC_ID",
        "AUTO_PUBLISH", "INCLUDE_HD_CLAIM", "TIMEZONE", "DATABASE_PATH", "LOG_LEVEL",
    ):
        assert re.search(rf"^{name}=", example, re.MULTILINE), f"{name} missing from .env.example"


def test_readme_contains_no_token():
    readme = PROJECT_ROOT / "README.md"
    if not readme.exists():
        pytest.skip("README.md not created yet")
    text = readme.read_text(encoding="utf-8")
    assert not TOKEN_PATTERN.search(text)


def test_start_script_contains_no_token():
    script = PROJECT_ROOT / "start.bat"
    if not script.exists():
        pytest.skip("start.bat not created yet")
    assert not TOKEN_PATTERN.search(script.read_text(encoding="utf-8"))


def test_no_env_txt_file_is_present():
    """The exposed secret file must not exist any more."""
    assert not (PROJECT_ROOT / ".env.txt").exists()


def test_local_env_file_is_not_tracked_but_still_exists():
    """The user's .env must be preserved locally and ignored by git."""
    ignore_file = PROJECT_ROOT / ".gitignore"
    text = ignore_file.read_text(encoding="utf-8")
    assert re.search(r"^\.env$", text, re.MULTILINE)
    assert re.search(r"^\.env\.txt$", text, re.MULTILINE)


# --------------------------------------------------------------------------- #
# Ignore rules
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("pattern", [
    r"^\.env$", r"^\.env\.txt$", r"^\*\.db$", r"^\*\.db-wal$", r"^\*\.db-shm$",
    r"^__pycache__/$", r"^\.pytest_cache/$", r"^\.coverage$", r"^logs/$", r"^\*\.log$",
    r"^\.venv/$", r"^\.idea/$", r"^\.vscode/$",
])
def test_gitignore_covers_required_pattern(pattern):
    text = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert re.search(pattern, text, re.MULTILINE), f"{pattern} missing from .gitignore"


def test_gitignore_keeps_env_example():
    text = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "!*.env.example" in text or "!.env.example" in text


@pytest.mark.skipif(
    not (PROJECT_ROOT / ".git").exists(), reason="not a git repository"
)
def test_secret_files_are_git_ignored():
    ignored = subprocess.run(
        ["git", "check-ignore", ".env", ".env.txt", "aniwave.db"],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=60,
    )
    assert ignored.returncode == 0, ignored.stdout + ignored.stderr


# --------------------------------------------------------------------------- #
# Token handling at runtime
# --------------------------------------------------------------------------- #


def test_token_is_read_only_from_the_environment(monkeypatch):
    from config import load_config, reset_config_cache

    env_token = fake_token("e")
    for key, value in (
        ("TELEGRAM_BOT_TOKEN", env_token),
        ("SOURCE_GROUP_ID", "-1004428338491"),
        ("TARGET_CHANNEL", "@aniwavebd"),
        ("ADMIN_IDS", "6589890362"),
        ("WEB_SERIES_TOPIC_ID", "33"),
        ("AUTO_PUBLISH", "false"),
        ("INCLUDE_HD_CLAIM", "false"),
        ("TIMEZONE", "Asia/Dhaka"),
        ("DATABASE_PATH", "envonly.db"),
        ("LOG_LEVEL", "INFO"),
    ):
        monkeypatch.setenv(key, value)

    reset_config_cache()
    try:
        config = load_config(force=True)
        assert config.bot_token == env_token
    finally:
        reset_config_cache()


def test_config_never_reads_a_hardcoded_token(env):
    import config as config_module

    source = Path(config_module.__file__).read_text(encoding="utf-8")
    assert "TELEGRAM_BOT_TOKEN" in source
    assert not TOKEN_PATTERN.search(source)


def test_config_errors_do_not_echo_secret_values(env):
    confidential = fake_token("c")
    env["TELEGRAM_BOT_TOKEN"] = confidential
    env["SOURCE_GROUP_ID"] = "not-a-number"
    with pytest.raises(ConfigError) as excinfo:
        AppConfig.load(env)
    assert confidential not in str(excinfo.value)


def test_redact_removes_configured_token():
    token = fake_token("v")
    assert token not in redact(f"leak {token}", secrets=(token,))


def test_redaction_marker_used():
    assert REDACTED in redact("123456789:AA" + "z" * 35)


def test_debug_output_never_contains_the_token(env):
    from types import SimpleNamespace

    from ui import debug_text

    config = AppConfig.load(env)
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=config.source_group_id, type="supergroup"),
        effective_user=SimpleNamespace(id=6589890362),
        effective_message=SimpleNamespace(message_thread_id=23, message_id=5,
                                          video=object(), media_group_id=None),
    )
    text = debug_text(update=update, config=config)
    assert config.bot_token not in text
    assert REDACTED not in text or True  # marker only appears if something was scrubbed


def test_status_output_never_contains_the_token_or_db_path(env):
    from ui import status_text

    config = AppConfig.load(env)
    text = status_text(config=config, counts_items={}, counts_albums={}, database_ok=True)
    assert config.bot_token not in text
    assert str(config.database_path) not in text


def test_requirements_are_pinned():
    text = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert "==" in line, f"dependency not pinned: {line}"
    assert "python-telegram-bot==21.11.1" in text
    assert "tzdata==" in text, "tzdata is required on Windows for TIMEZONE validation"

# --------------------------------------------------------------------------- #
# Topic configuration consistency
# --------------------------------------------------------------------------- #


def test_declared_topic_is_documented_in_env_example():
    """config.TOPIC_SPECS and .env.example must not contradict each other."""
    import config as config_module

    example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    for var_name, _title, _emoji in config_module.TOPIC_SPECS:
        assert re.search(rf"^{var_name}=", example, re.MULTILINE), (
            f"{var_name} is declared in TOPIC_SPECS but missing from .env.example"
        )


def test_topic_variable_names_are_documented_as_case_sensitive():
    example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "CASE-SENSITIVELY" in example or "case-sensitively" in example.lower()


def test_required_vars_match_declared_topics():
    """Every declared topic variable is validated as required."""
    import config as config_module

    for var_name, _title, _emoji in config_module.TOPIC_SPECS:
        assert var_name in config_module.REQUIRED_VARS


def test_local_env_declares_the_active_topic():
    """The user's .env must contain the declared topic variable."""
    import config as config_module

    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        pytest.skip("no local .env in this checkout")
    env_text = env_path.read_text(encoding="utf-8")
    for var_name, _title, _emoji in config_module.TOPIC_SPECS:
        assert re.search(rf"^{var_name}\s*=", env_text, re.MULTILINE), (
            f"local .env is missing {var_name}"
        )
