"""Configuration loading, strict validation, and secret-safety helpers.

Design notes
------------
* No import-time side effects beyond loading ``.env``. Configuration is parsed
  lazily through :func:`load_config` so that importing this module never raises
  and tests can inject an explicit environment mapping.
* Validation is *fail fast* and never prints secret values.
* ``TARGET_CHANNEL`` is always kept as a string (``@aniwavebd`` must never be
  coerced into an integer).
* :func:`redact` is used by the logging layer so that a token can never reach a
  log record or a Telegram admin message.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

APP_VERSION = "1.0.0"

REQUIRED_VARS: Tuple[str, ...] = (
    "TELEGRAM_BOT_TOKEN",
    "SOURCE_GROUP_ID",
    "TARGET_CHANNEL",
    "ADMIN_IDS",
    "ONE_PIECE_TOPIC_ID",
    "NARUTO_TOPIC_ID",
    "BLEACH_TOPIC_ID",
    "AUTO_PUBLISH",
    "INCLUDE_HD_CLAIM",
    "TIMEZONE",
    "DATABASE_PATH",
    "LOG_LEVEL",
)

OPTIONAL_VARS: Tuple[str, ...] = ("ALBUM_QUIET_SECONDS",)

TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
FALSE_VALUES = frozenset({"false", "0", "no", "off"})
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

DEFAULT_ALBUM_QUIET_SECONDS = 3
MIN_ALBUM_QUIET_SECONDS = 1
MAX_ALBUM_QUIET_SECONDS = 120


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid.

    The message never contains a secret value.
    """


# --------------------------------------------------------------------------- #
# Secret handling
# --------------------------------------------------------------------------- #

#: Shape of a Telegram bot token. Used for leak detection and log redaction.
TOKEN_PATTERN = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,40}\b")

REDACTED = "***REDACTED***"


def redact(text: object, secrets: Iterable[str] = ()) -> str:
    """Return *text* with secrets replaced by :data:`REDACTED`.

    Both literal known secrets and anything *shaped* like a bot token are
    scrubbed, so an unexpected token can never be logged verbatim.
    """
    value = "" if text is None else str(text)
    for secret in secrets:
        if secret and len(secret) >= 8 and secret in value:
            value = value.replace(secret, REDACTED)
    return TOKEN_PATTERN.sub(REDACTED, value)


def scan_for_secrets(
    root: str = ".",
    *,
    skip_names: Sequence[str] = (".env",),
    skip_dirs: Sequence[str] = (".git", "__pycache__", ".venv", "venv", ".pytest_cache", ".mypy_cache"),
    suffixes: Sequence[str] = (".py", ".txt", ".md", ".cfg", ".ini", ".toml", ".json", ".yml", ".yaml", ".bat", ".ps1", ".example", ""),
) -> List[str]:
    """Return ``path:line`` findings for files containing token-like strings.

    The token value itself is never returned - only the location - so the result
    is safe to log.
    """
    findings: List[str] = []
    skip = set(skip_names)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for filename in filenames:
            if filename in skip or filename.startswith(".env"):
                continue
            if not filename.endswith(tuple(suffixes)):
                continue
            path = os.path.join(dirpath, filename)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                    for lineno, line in enumerate(handle, start=1):
                        if TOKEN_PATTERN.search(line):
                            findings.append(f"{path}:{lineno}")
            except OSError:
                continue
    return findings


# --------------------------------------------------------------------------- #
# Topic registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TopicConfig:
    """Anime metadata bound to a Telegram forum topic id."""

    topic_id: int
    title: str
    emoji: str

    @property
    def slug(self) -> str:
        return self.title.lower().replace(" ", "_")


#: Single source of truth for topic metadata. Adding an anime only requires a
#: new entry here plus a matching environment variable.
TOPIC_SPECS: Tuple[Tuple[str, str, str], ...] = (
    ("ONE_PIECE_TOPIC_ID", "One Piece", "\U0001F4FA"),
    ("NARUTO_TOPIC_ID", "Naruto", "\U0001F365"),
    ("BLEACH_TOPIC_ID", "Bleach", "⚔️"),
)


def build_topics(env: Mapping[str, str]) -> Tuple[TopicConfig, ...]:
    """Build a fresh, immutable topic tuple from *env*.

    A new tuple is returned on every call, so repeated initialisation can never
    observe stale topic state.
    """
    topics: List[TopicConfig] = []
    seen: Dict[int, str] = {}
    for var_name, title, emoji in TOPIC_SPECS:
        raw = _require(env, var_name)
        topic_id = _parse_topic_id(var_name, raw)
        if topic_id in seen:
            raise ConfigError(
                f"{var_name}: topic id {topic_id} is already mapped to {seen[topic_id]!r}. "
                f"Topic ids must be unique."
            )
        seen[topic_id] = title
        topics.append(TopicConfig(topic_id=topic_id, title=title, emoji=emoji))
    return tuple(topics)


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #


def _require(env: Mapping[str, str], name: str) -> str:
    raw = env.get(name)
    if raw is None:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill in every value. "
            f"Never commit a real token."
        )
    value = raw.strip()
    if not value:
        raise ConfigError(f"{name} is set but blank. Provide a value in .env.")
    return value


def _parse_topic_id(name: str, raw: str) -> int:
    if not raw.isdigit():
        raise ConfigError(
            f"{name} must be a positive integer topic id (got {raw!r}). "
            f"Send /topicid inside the topic to discover it."
        )
    topic_id = int(raw)
    if topic_id <= 0:
        raise ConfigError(f"{name} must be a positive integer (got {topic_id}).")
    return topic_id


def _parse_bool(name: str, raw: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in TRUE_VALUES:
        return True
    if lowered in FALSE_VALUES:
        return False
    raise ConfigError(
        f"{name} must be one of true/false/1/0/yes/no/on/off (got {raw!r})."
    )


def _parse_admin_ids(name: str, raw: str) -> List[int]:
    parts = [chunk.strip() for chunk in raw.split(",")]
    parts = [chunk for chunk in parts if chunk]
    if not parts:
        raise ConfigError(f"{name} must list at least one numeric Telegram user id.")
    admin_ids: List[int] = []
    for chunk in parts:
        if not chunk.isdigit():
            raise ConfigError(
                f"{name} contains a non-numeric entry {chunk!r}. "
                f"Use comma-separated numeric ids, for example 123456789."
            )
        value = int(chunk)
        if value <= 0:
            raise ConfigError(f"{name} contains a non-positive id {value}.")
        if value not in admin_ids:
            admin_ids.append(value)
    return admin_ids


def _parse_source_group_id(name: str, raw: str) -> int:
    candidate = raw[1:] if raw.startswith("-") else raw
    if not candidate.isdigit():
        raise ConfigError(
            f"{name} must be a numeric Telegram chat id such as -1001234567890 (got {raw!r})."
        )
    value = int(raw)
    if value == 0:
        raise ConfigError(f"{name} must not be 0.")
    return value


def _parse_target_channel(name: str, raw: str) -> str:
    """Validate the channel reference but always return it as a string."""
    if raw.startswith("@"):
        username = raw[1:]
        if not re.fullmatch(r"[A-Za-z0-9_]{4,}", username):
            raise ConfigError(
                f"{name} must look like @channelusername or a -100... chat id (got {raw!r})."
            )
        return raw
    candidate = raw[1:] if raw.startswith("-") else raw
    if candidate.isdigit():
        return raw
    raise ConfigError(
        f"{name} must be @channelusername or a numeric -100... chat id (got {raw!r})."
    )


def _parse_log_level(name: str, raw: str) -> str:
    level = raw.strip().upper()
    if level not in LOG_LEVELS:
        raise ConfigError(f"{name} must be one of {', '.join(LOG_LEVELS)} (got {raw!r}).")
    return level


def _parse_timezone(name: str, raw: str) -> str:
    try:
        ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ConfigError(f"{name} is not a known IANA timezone (got {raw!r}).") from exc
    return raw


def _parse_quiet_seconds(env: Mapping[str, str]) -> int:
    raw = (env.get("ALBUM_QUIET_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_ALBUM_QUIET_SECONDS
    if not raw.isdigit():
        raise ConfigError(
            f"ALBUM_QUIET_SECONDS must be an integer between "
            f"{MIN_ALBUM_QUIET_SECONDS} and {MAX_ALBUM_QUIET_SECONDS} (got {raw!r})."
        )
    value = int(raw)
    if not MIN_ALBUM_QUIET_SECONDS <= value <= MAX_ALBUM_QUIET_SECONDS:
        raise ConfigError(
            f"ALBUM_QUIET_SECONDS must be between {MIN_ALBUM_QUIET_SECONDS} and "
            f"{MAX_ALBUM_QUIET_SECONDS} (got {value})."
        )
    return value


# --------------------------------------------------------------------------- #
# AppConfig
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AppConfig:
    """Fully validated runtime configuration."""

    bot_token: str
    source_group_id: int
    target_channel: str
    admin_ids: Tuple[int, ...]
    auto_publish: bool
    include_hd_claim: bool
    timezone: str
    database_path: str
    log_level: str
    album_quiet_seconds: int = DEFAULT_ALBUM_QUIET_SECONDS
    topics: Tuple[TopicConfig, ...] = ()

    @classmethod
    def load(cls, env: Optional[Mapping[str, str]] = None) -> "AppConfig":
        """Parse and validate configuration, raising :class:`ConfigError`.

        Every problem found is reported, not just the first one, so a user
        fixing their ``.env`` sees all of it at once. Secret values are never
        echoed back.
        """
        source: Mapping[str, str] = os.environ if env is None else env

        problems: List[str] = []
        values: Dict[str, object] = {}

        def attempt(name: str, parser, *args):
            try:
                values[name] = parser(*args)
            except ConfigError as exc:
                problems.append(str(exc))

        # token -------------------------------------------------------------
        token: Optional[str] = None
        try:
            token = _require(source, "TELEGRAM_BOT_TOKEN")
        except ConfigError as exc:
            problems.append(str(exc))

        # simple scalars ----------------------------------------------------
        for name in ("SOURCE_GROUP_ID", "TARGET_CHANNEL", "AUTO_PUBLISH",
                     "INCLUDE_HD_CLAIM", "TIMEZONE", "DATABASE_PATH", "LOG_LEVEL"):
            try:
                raw = _require(source, name)
            except ConfigError as exc:
                problems.append(str(exc))
                continue
            values[name] = raw

        # admin ids ---------------------------------------------------------
        admin_ids: Optional[Tuple[int, ...]] = None
        try:
            admin_ids = tuple(_parse_admin_ids("ADMIN_IDS", _require(source, "ADMIN_IDS")))
        except ConfigError as exc:
            problems.append(str(exc))

        # topics ------------------------------------------------------------
        topics: Tuple[TopicConfig, ...] = ()
        try:
            topics = build_topics(source)
        except ConfigError as exc:
            problems.append(str(exc))

        # typed scalars -----------------------------------------------------
        source_group_id: Optional[int] = None
        if isinstance(values.get("SOURCE_GROUP_ID"), str):
            attempt("SOURCE_GROUP_ID", _parse_source_group_id, "SOURCE_GROUP_ID", values["SOURCE_GROUP_ID"])
            source_group_id = values.get("SOURCE_GROUP_ID")  # type: ignore[assignment]

        target_channel: Optional[str] = None
        if isinstance(values.get("TARGET_CHANNEL"), str):
            attempt("TARGET_CHANNEL", _parse_target_channel, "TARGET_CHANNEL", values["TARGET_CHANNEL"])
            target_channel = values.get("TARGET_CHANNEL")  # type: ignore[assignment]

        auto_publish = False
        if isinstance(values.get("AUTO_PUBLISH"), str):
            attempt("AUTO_PUBLISH", _parse_bool, "AUTO_PUBLISH", values["AUTO_PUBLISH"])
            auto_publish = bool(values.get("AUTO_PUBLISH"))

        include_hd_claim = False
        if isinstance(values.get("INCLUDE_HD_CLAIM"), str):
            attempt("INCLUDE_HD_CLAIM", _parse_bool, "INCLUDE_HD_CLAIM", values["INCLUDE_HD_CLAIM"])
            include_hd_claim = bool(values.get("INCLUDE_HD_CLAIM"))

        timezone: Optional[str] = None
        if isinstance(values.get("TIMEZONE"), str):
            attempt("TIMEZONE", _parse_timezone, "TIMEZONE", values["TIMEZONE"])
            timezone = values.get("TIMEZONE")  # type: ignore[assignment]

        log_level: Optional[str] = None
        if isinstance(values.get("LOG_LEVEL"), str):
            attempt("LOG_LEVEL", _parse_log_level, "LOG_LEVEL", values["LOG_LEVEL"])
            log_level = values.get("LOG_LEVEL")  # type: ignore[assignment]

        database_path = values.get("DATABASE_PATH") if isinstance(values.get("DATABASE_PATH"), str) else None

        album_quiet_seconds = DEFAULT_ALBUM_QUIET_SECONDS
        try:
            album_quiet_seconds = _parse_quiet_seconds(source)
        except ConfigError as exc:
            problems.append(str(exc))

        if problems:
            raise ConfigError(
                "Invalid configuration:\n  - " + "\n  - ".join(problems)
            )

        assert token is not None and source_group_id is not None
        assert target_channel is not None and admin_ids is not None
        assert timezone is not None and log_level is not None and database_path is not None

        return cls(
            bot_token=token,
            source_group_id=source_group_id,
            target_channel=target_channel,
            admin_ids=admin_ids,
            auto_publish=auto_publish,
            include_hd_claim=include_hd_claim,
            timezone=timezone,
            database_path=database_path,
            log_level=log_level,
            album_quiet_seconds=album_quiet_seconds,
            topics=topics,
        )

    # -- helpers ---------------------------------------------------------- #

    def is_admin(self, user_id: Optional[int]) -> bool:
        return user_id is not None and user_id in self.admin_ids

    def topic_for(self, topic_id: Optional[int]) -> Optional[TopicConfig]:
        """Return the topic metadata for *topic_id*, or ``None``."""
        if topic_id is None:
            return None
        for topic in self.topics:
            if topic.topic_id == topic_id:
                return topic
        return None

    def topic_ids(self) -> List[int]:
        return sorted(topic.topic_id for topic in self.topics)

    def topic_label(self, topic_id: Optional[int]) -> str:
        topic = self.topic_for(topic_id)
        return f"{topic.emoji} {topic.title}" if topic else f"topic {topic_id}"

    def secrets(self) -> Tuple[str, ...]:
        return (self.bot_token,)


# --------------------------------------------------------------------------- #
# Lazy singleton
# --------------------------------------------------------------------------- #

_cached: Optional[AppConfig] = None


def load_config(env: Optional[Mapping[str, str]] = None, *, force: bool = False) -> AppConfig:
    """Load, validate and cache the configuration."""
    global _cached
    if env is None and _cached is not None and not force:
        return _cached
    config = AppConfig.load(env)
    if env is None:
        _cached = config
    return config


def get_config() -> AppConfig:
    """Return the cached configuration, loading it on first use."""
    return load_config()


def reset_config_cache() -> None:
    """Drop the cached configuration (used by tests)."""
    global _cached
    _cached = None


def load_dotenv_file() -> bool:
    """Load ``.env`` into ``os.environ``. Returns True when a file was found."""
    return bool(load_dotenv())