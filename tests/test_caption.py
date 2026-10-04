import pytest
from caption import parse_episode, generate_caption, validate_caption_length


class TestParseEpisode:
    def test_integer_episode(self):
        assert parse_episode("One Piece EP 1165") == "1165"
        assert parse_episode("Episode 25") == "25"

    def test_decimal_episode(self):
        assert parse_episode("One Piece EP 12.5") == "12.5"
        assert parse_episode("Episode 1.5") == "1.5"

    def test_standalone_number(self):
        assert parse_episode("1165") == "1165"
        assert parse_episode("25") == "25"

    def test_case_insensitive(self):
        assert parse_episode("one piece ep 100") == "100"
        assert parse_episode("ONE PIECE EPISODE 200") == "200"

    def test_leading_zeros(self):
        result = parse_episode("EP 00125")
        assert result == "125"

    def test_no_episode(self):
        assert parse_episode("Hello world") is None
        assert parse_episode("") is None
        assert parse_episode(None) is None

    def test_naruto_episode(self):
        assert parse_episode("Naruto EP 25") == "25"

    def test_bleach_episode(self):
        assert parse_episode("Bleach Episode 12") == "12"

    def test_ep_with_colon(self):
        assert parse_episode("EP: 42") == "42"

    def test_episode_with_dot(self):
        assert parse_episode("EP. 7") == "7"


class TestGenerateCaption:
    def test_basic_caption(self):
        caption = generate_caption("📺", "One Piece", "1165")
        assert "One Piece" in caption
        assert "1165" in caption
        assert "🔥 NEW EPISODE RELEASED 🔥" in caption

    def test_hd_claim_included(self):
        caption = generate_caption("📺", "One Piece", "1165", include_hd_claim=True)
        assert "✅ HD Quality" in caption

    def test_hd_claim_excluded(self):
        caption = generate_caption("📺", "One Piece", "1165", include_hd_claim=False)
        assert "✅ HD Quality" not in caption

    def test_under_limit(self):
        caption = generate_caption("📺", "One Piece", "1165")
        assert validate_caption_length(caption) is True

    def test_empty_emoji_title(self):
        caption = generate_caption("", "", "1")
        assert caption is not None
        assert len(caption) > 0


class TestValidateCaptionLength:
    def test_short_caption(self):
        assert validate_caption_length("Short caption") is True

    def test_exact_limit(self):
        caption = "a" * 1024
        assert validate_caption_length(caption) is True

    def test_over_limit(self):
        caption = "a" * 1025
        assert validate_caption_length(caption) is False
