"""Episode parsing/validation and caption generation (Phase D / N items 8-9)."""

from __future__ import annotations

import pytest

from caption import (
    CAPTION_LIMIT,
    MAX_EPISODE_DECIMALS,
    EpisodeValidationError,
    fit_caption,
    generate_caption,
    normalize_episode,
    parse_episode,
    validate_caption_length,
    validate_episode_input,
)


# --------------------------------------------------------------------------- #
# parse_episode - the formats the product must support
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("caption,expected", [
    ("One Piece EP 1165", "1165"),
    ("One Piece Episode 1165", "1165"),
    ("EP 1165", "1165"),
    ("Episode 25", "25"),
    ("1165", "1165"),
    ("12.5", "12.5"),
    ("Naruto EP 25", "25"),
    ("Bleach Episode 12", "12"),
    ("one piece episode 12.5", "12.5"),
    ("Naruto Episode 220", "220"),
    ("Bleach EP 366", "366"),
])
def test_supported_caption_forms(caption, expected):
    assert parse_episode(caption) == expected


@pytest.mark.parametrize("caption,expected", [
    ("EP: 42", "42"),
    ("Ep. 7", "7"),
    ("EP 00125", "125"),
    ("ep1165", "1165"),
    ("EP=1165", "1165"),
    ("12.50", "12.5"),
    ("  1165  ", "1165"),
])
def test_notation_variants(caption, expected):
    assert parse_episode(caption) == expected


@pytest.mark.parametrize("caption,expected", [
    ("One Piece 1165", "1165"),
    ("One Piece 1165 1080p", "1165"),
    ("One Piece EP 1165 1080p", "1165"),
    ("One Piece 1165 - New Episode", "1165"),
    ("OP 1165 720p x264", "1165"),
])
def test_resolution_noise_is_not_an_episode(caption, expected):
    """A trailing 1080p must never be read as the episode number."""
    assert parse_episode(caption) == expected


@pytest.mark.parametrize("caption", [
    None, "", "   ", "No numbers here", "EP", "Episode", "Coming soon",
    "EP 0", "EP -3", "one piece",
])
def test_captions_without_a_usable_episode(caption):
    assert parse_episode(caption) is None


def test_episode_zero_is_not_accepted():
    assert parse_episode("EP 0") is None


# --------------------------------------------------------------------------- #
# normalize_episode
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw,expected", [
    ("1165", "1165"),
    ("00125", "125"),
    ("12.5", "12.5"),
    ("12.50", "12.5"),
    ("12.0", "12"),
    ("0.5", "0.5"),
    ("0007", "7"),
    (7, "7"),
])
def test_normalize(raw, expected):
    assert normalize_episode(raw) == expected


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "", "nan", "inf"])
def test_normalize_rejects_invalid(raw):
    with pytest.raises(EpisodeValidationError):
        normalize_episode(raw)


def test_normalize_rejects_excessive_precision():
    too_precise = "1." + "0" * (MAX_EPISODE_DECIMALS + 1)
    with pytest.raises(EpisodeValidationError) as excinfo:
        normalize_episode(too_precise)
    assert "decimal places" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# validate_episode_input - strict admin input (Edit Episode flow)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("text,expected", [
    ("1165", "1165"),
    ("12.5", "12.5"),
    ("ep 25", "25"),
    ("EP 25", "25"),
    ("Episode 3.5", "3.5"),
    ("  42  ", "42"),
    ("00125", "125"),
])
def test_validate_episode_input_accepts(text, expected):
    assert validate_episode_input(text) == expected


@pytest.mark.parametrize("text", [
    None, "", "   ", "0", "-1", "-0.5", "abc", "12.5 and more",
    "1e5", "nan", "Infinity", "EP 12 blah", "hello", "12,5",
])
def test_validate_episode_input_rejects(text):
    with pytest.raises(EpisodeValidationError):
        validate_episode_input(text)


def test_validate_episode_input_rejects_excessive_precision():
    with pytest.raises(EpisodeValidationError) as excinfo:
        validate_episode_input("1." + "0" * (MAX_EPISODE_DECIMALS + 1))
    assert "decimal places" in str(excinfo.value)


def test_validation_error_message_is_admin_safe():
    with pytest.raises(EpisodeValidationError) as excinfo:
        validate_episode_input("not an episode")
    message = str(excinfo.value)
    assert "not an episode" in message
    assert "Traceback" not in message


# --------------------------------------------------------------------------- #
# generate_caption
# --------------------------------------------------------------------------- #


def test_caption_contains_expected_sections():
    caption = generate_caption("\U0001F4FA", "One Piece", "1165", include_hd_claim=False)
    assert "NEW EPISODE RELEASED" in caption
    assert "One Piece" in caption
    assert "Episode: 1165" in caption
    assert "Audio: Japanese" in caption
    assert "Subtitle: Bangla" in caption
    assert "Channel: @aniwavebd" in caption
    assert "Regular Updates" in caption


def test_hd_claim_respects_configuration():
    with_hd = generate_caption("\U0001F4FA", "One Piece", "1165", include_hd_claim=True)
    without_hd = generate_caption("\U0001F4FA", "One Piece", "1165", include_hd_claim=False)
    assert "HD Quality" in with_hd
    assert "HD Quality" not in without_hd


def test_caption_uses_configured_channel():
    caption = generate_caption("\U0001F4FA", "Naruto", "25", channel="@otherchannel")
    assert "Channel: @otherchannel" in caption


def test_caption_fits_limit():
    caption = generate_caption("\U0001F4FA", "One Piece", "1165", include_hd_claim=True)
    assert validate_caption_length(caption)
    assert len(caption) <= CAPTION_LIMIT


def test_oversized_title_is_shortened_not_dropped_whole():
    """Optional sections are shed before the required content is lost."""
    caption = generate_caption("\U0001F4FA", "X" * 2000, "1165", include_hd_claim=True)
    assert len(caption) <= CAPTION_LIMIT
    assert "Episode: 1165" in caption


def test_oversized_caption_still_keeps_required_lines():
    caption = generate_caption("\U0001F4FA", "Y" * 1100, "999", include_hd_claim=True)
    assert len(caption) <= CAPTION_LIMIT
    assert "Episode: 999" in caption
    assert "Audio: Japanese" in caption
    assert "Subtitle: Bangla" in caption


def test_fit_caption_truncates():
    assert len(fit_caption("z" * (CAPTION_LIMIT + 50))) == CAPTION_LIMIT
    assert fit_caption("short") == "short"


def test_validate_caption_length_boundaries():
    assert validate_caption_length("a" * CAPTION_LIMIT) is True
    assert validate_caption_length("a" * (CAPTION_LIMIT + 1)) is False


def test_caption_is_plain_text_without_markdown_reserved_syntax():
    """Captions contain '.' and '-' but are sent without a parse mode."""
    caption = generate_caption("\U0001F4FA", "One Piece", "12.5", include_hd_claim=True)
    assert "." in caption and "@" in caption
    assert "*" not in caption
    assert "_" not in caption