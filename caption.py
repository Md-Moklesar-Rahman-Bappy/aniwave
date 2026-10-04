"""Episode-number parsing/validation and plain-text channel caption generation.

Captions are always produced as **plain text**. MarkdownV2 is deliberately not
used anywhere: the generated Bengali caption contains characters that
MarkdownV2 treats as reserved (``.``, ``-``, ``@``), which previously caused
``BadRequest`` failures at publish time.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Iterable, List, Optional, Sequence

#: Telegram's documented caption limit for media messages.
CAPTION_LIMIT = 1024

#: Maximum number of decimal places accepted for an episode number.
MAX_EPISODE_DECIMALS = 4

# ``EP 1165`` / ``episode:25`` / ``Ep.12.5``
# NOTE: a hyphen is intentionally *not* accepted as a separator, otherwise
# ``EP -3`` would be mis-read as episode 3 instead of being rejected.
_EPISODE_LABELLED = re.compile(
    r"\bep(?:isode)?\s*\.?\s*[:=]?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
# A caption that is *only* a number: ``1165`` / ``12.5``
_BARE_NUMBER = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*$")
# Any number anywhere, used as a last-resort fallback. A leading minus sign is
# excluded so that ``EP -3`` is not silently read as episode 3.
_ANY_NUMBER = re.compile(r"(?<![\d.\-])(\d+(?:\.\d+)?)")

# Resolution / quality noise that must never be mistaken for an episode number.
_NOISE = re.compile(
    r"\b\d{3,4}\s*[pi]\b"          # 1080p, 720p
    r"|\bx26[45]\b"                # x264, x265
    r"|\b(?:hd|sd|fhd|uhd|bdrip|web[- ]?dl|webrip|bluray|10bit|8bit)\b",
    re.IGNORECASE,
)


class EpisodeValidationError(ValueError):
    """Raised when user-supplied episode input cannot be accepted.

    The message is safe to show to an admin.
    """


def _strip_leading_zeros(raw: str) -> str:
    if "." in raw:
        whole, _, frac = raw.partition(".")
        whole = whole.lstrip("0") or "0"
        frac = frac.rstrip("0")
        return f"{whole}.{frac}" if frac else whole
    return raw.lstrip("0") or "0"


def normalize_episode(raw: str) -> str:
    """Normalise a numeric episode string.

    ``"00125" -> "125"``, ``"12.50" -> "12.5"``, ``"12.0" -> "12"``.
    Raises :class:`EpisodeValidationError` for non-positive or non-finite input.
    """
    text = str(raw).strip()
    if not text:
        raise EpisodeValidationError("Episode number is empty.")
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise EpisodeValidationError(f"{text!r} is not a number.") from exc
    if not value.is_finite():
        raise EpisodeValidationError("Episode number must be finite.")
    if value <= 0:
        raise EpisodeValidationError("Episode number must be greater than zero.")
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -MAX_EPISODE_DECIMALS:
        raise EpisodeValidationError(
            f"Episode number allows at most {MAX_EPISODE_DECIMALS} decimal places."
        )
    normalized = format(value.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return _strip_leading_zeros(normalized)


def parse_episode(caption: Optional[str]) -> Optional[str]:
    """Extract an episode number from an upload caption.

    Understands ``One Piece EP 1165``, ``One Piece Episode 1165``, ``EP 1165``,
    ``Episode 25``, ``12.5`` and a bare ``1165``. Returns ``None`` when no
    usable episode number is present or when the candidate is not a valid
    positive episode number.
    """
    if not caption:
        return None
    text = caption.strip()
    if not text:
        return None

    for pattern in (_EPISODE_LABELLED, _BARE_NUMBER):
        match = pattern.search(text)
        if match:
            return _safe_normalize(match.group(1))

    # Last-resort fallback for captions with no explicit marker, e.g.
    # "One Piece 1165". Resolution/quality noise is stripped first so that
    # "One Piece 1165 1080p" resolves to 1165 rather than 1080.
    cleaned = _NOISE.sub(" ", text)
    match = _ANY_NUMBER.search(cleaned)
    if match:
        return _safe_normalize(match.group(1))
    return None


def _safe_normalize(candidate: str) -> Optional[str]:
    try:
        return normalize_episode(candidate)
    except EpisodeValidationError:
        return None


def validate_episode_input(text: Optional[str]) -> str:
    """Strictly validate admin-supplied episode input (Edit Episode flow).

    Accepts a bare number or an ``EP``/``Episode``-prefixed number. Rejects
    zero, negatives, NaN/infinity, excessive precision and unrelated text.
    """
    if text is None:
        raise EpisodeValidationError("Send an episode number, for example 1165 or 12.5.")
    cleaned = text.strip()
    if not cleaned:
        raise EpisodeValidationError("Send an episode number, for example 1165 or 12.5.")

    candidate: Optional[str] = None
    match = _EPISODE_LABELLED.search(cleaned)
    if match:
        candidate = match.group(1)
        residue = _EPISODE_LABELLED.sub("", cleaned).strip()
        if residue:
            raise EpisodeValidationError(
                f"I could not read {text!r}. Send only the episode number, e.g. 1165 or 12.5."
            )
    else:
        bare = _BARE_NUMBER.match(cleaned)
        candidate = bare.group(1) if bare else None

    if candidate is None:
        raise EpisodeValidationError(
            f"I could not read {text!r}. Send a positive number, e.g. 1165 or 12.5."
        )
    return normalize_episode(candidate)


# --------------------------------------------------------------------------- #
# Caption generation
# --------------------------------------------------------------------------- #

_SEPARATOR = "━" * 14


def _required_lines(emoji: str, anime_title: str, episode_number: str, channel: str) -> List[str]:
    return [
        "\U0001F525 NEW EPISODE RELEASED \U0001F525",
        "",
        f"{emoji} {anime_title}".strip(),
        "",
        f"\U0001F4FA Episode: {episode_number}",
        "\U0001F399 Audio: Japanese",
        "\U0001F4AC Subtitle: Bangla",
    ]


def _optional_blocks(include_hd_claim: bool, channel: str) -> List[List[str]]:
    """Optional blocks ordered from most to least important."""
    quality: List[str] = []
    if include_hd_claim:
        quality.append("✅ HD Quality")
    quality.append("✅ Regular Updates")

    blocks: List[List[str]] = [
        ["", _SEPARATOR, *quality, _SEPARATOR],
        ["", f"\U0001F4E2 Channel: {channel}"],
    ]
    return blocks


def generate_caption(
    emoji: str,
    anime_title: str,
    episode_number: str,
    include_hd_claim: bool = False,
    channel: str = "@aniwavebd",
) -> str:
    """Build the Bengali publication caption as plain text.

    If the result would exceed :data:`CAPTION_LIMIT`, optional blocks are
    dropped from least to most important until it fits. The required header,
    episode, audio and subtitle lines are never dropped.
    """
    required = _required_lines(emoji, anime_title, episode_number, channel)
    blocks = _optional_blocks(include_hd_claim, channel)

    lines = list(required)
    for block in blocks:
        candidate = lines + block
        text = "\n".join(candidate)
        if len(text) <= CAPTION_LIMIT:
            lines = candidate
    text = "\n".join(lines)
    if len(text) > CAPTION_LIMIT:  # pragma: no cover - defensive
        text = "\n".join(required)[:CAPTION_LIMIT]
    return text


def validate_caption_length(caption: str) -> bool:
    return len(caption) <= CAPTION_LIMIT


def fit_caption(caption: str) -> str:
    """Truncate *caption* so it never exceeds :data:`CAPTION_LIMIT`."""
    if len(caption) <= CAPTION_LIMIT:
        return caption
    return caption[:CAPTION_LIMIT]