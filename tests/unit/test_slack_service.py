"""Unit tests for services/slack.py."""

import pytest

from models import ReleaseMetadata
from services.slack import build_simple_slack_blocks, build_slack_blocks
from tests.factories import make_library_item, make_release_metadata


@pytest.fixture
def sample_library_item():
    """Create a sample library item."""
    return make_library_item()


@pytest.fixture
def sample_discogs_result():
    """Create a sample Discogs search result."""
    return make_release_metadata(
        release_id=12345,
        artwork_url="https://example.com/artwork.jpg",
    )


class TestBuildSlackBlocks:
    """Tests for build_slack_blocks function."""

    def test_builds_blocks_with_single_item(self, sample_library_item, sample_discogs_result):
        """Test building blocks with a single library item and artwork."""
        blocks = build_slack_blocks(
            message="Here's what I found:",
            items_with_artwork=[(sample_library_item, sample_discogs_result)],
        )

        assert len(blocks) >= 2  # Header + item block
        assert blocks[0]["type"] == "section"
        assert "*Here's what I found:*" in blocks[0]["text"]["text"]

        # Check item block has artist and title
        item_block = blocks[1]
        assert "Stereolab" in item_block["text"]["text"]
        assert "Aluminum Tunes" in item_block["text"]["text"]

        # Check artwork is included
        assert "accessory" in item_block
        assert item_block["accessory"]["type"] == "image"
        assert item_block["accessory"]["image_url"] == "https://example.com/artwork.jpg"

    def test_builds_blocks_without_artwork(self, sample_library_item):
        """Test building blocks without artwork."""
        blocks = build_slack_blocks(
            message="Here's what I found:",
            items_with_artwork=[(sample_library_item, None)],
        )

        assert len(blocks) >= 2
        item_block = blocks[1]
        assert "accessory" not in item_block

    def test_builds_blocks_with_context(self, sample_library_item):
        """Test building blocks with context message."""
        blocks = build_slack_blocks(
            message="Here's what I found:",
            items_with_artwork=[(sample_library_item, None)],
            context="Song not found, showing artist albums instead",
        )

        assert len(blocks) >= 3
        # Context should be second block
        assert "Song not found" in blocks[1]["text"]["text"]

    def test_builds_blocks_with_multiple_items(self, sample_library_item):
        """Test building blocks with multiple items."""
        item2 = make_library_item(
            id=2,
            artist="Cat Power",
            title="Moon Pix",
            call_letters="C",
            release_call_number=2,
        )

        blocks = build_slack_blocks(
            message="Found multiple albums:",
            items_with_artwork=[(sample_library_item, None), (item2, None)],
        )

        # Header + 2 items
        assert len(blocks) >= 3

    def test_builds_blocks_with_discogs_and_library_links(
        self, sample_library_item, sample_discogs_result
    ):
        """Test that blocks include Discogs and library links."""
        blocks = build_slack_blocks(
            message="Result:",
            items_with_artwork=[(sample_library_item, sample_discogs_result)],
        )

        item_block = blocks[1]
        text = item_block["text"]["text"]

        assert "Discogs" in text
        assert "WXYC" in text
        assert "discogs.com" in text

    @pytest.mark.parametrize(
        "streaming_field",
        ["spotify_url", "apple_music_url", "youtube_music_url", "bandcamp_url", "soundcloud_url"],
    )
    def test_builds_blocks_with_preview_link(self, sample_library_item, streaming_field):
        """Test that a Preview link appears when a streaming URL is available."""
        fields = {
            "artist": "Stereolab",
            "album": "Aluminum Tunes",
            "artwork_url": "https://example.com/artwork.jpg",
            "release_id": 99999,
            "release_url": "https://www.discogs.com/release/99999",
            streaming_field: "https://example.com/stream",
        }
        artwork = ReleaseMetadata.model_validate(fields)

        blocks = build_slack_blocks(
            message="Result:",
            items_with_artwork=[(sample_library_item, artwork)],
        )

        text = blocks[1]["text"]["text"]
        assert "<https://example.com/stream|Preview>" in text

    def test_preview_link_priority_order(self, sample_library_item):
        """Test that Preview link uses the first available URL in priority order."""
        artwork = ReleaseMetadata(
            artist="Stereolab",
            album="Aluminum Tunes",
            release_id=99999,
            release_url="https://www.discogs.com/release/99999",
            spotify_url="https://open.spotify.com/search/stereolab",
            youtube_music_url="https://music.youtube.com/search?q=stereolab",
        )

        blocks = build_slack_blocks(
            message="Result:",
            items_with_artwork=[(sample_library_item, artwork)],
        )

        text = blocks[1]["text"]["text"]
        assert "<https://open.spotify.com/search/stereolab|Preview>" in text

    def test_no_preview_link_without_streaming_urls(
        self, sample_library_item, sample_discogs_result
    ):
        """Test that no Preview link appears when no streaming URLs are set."""
        blocks = build_slack_blocks(
            message="Result:",
            items_with_artwork=[(sample_library_item, sample_discogs_result)],
        )

        text = blocks[1]["text"]["text"]
        assert "Preview" not in text

    def test_builds_blocks_handles_missing_artist(self):
        """Test building blocks when artist is None."""
        item = make_library_item(
            id=1,
            artist=None,
            title="Unknown Album",
            call_letters="X",
            genre=None,
            format=None,
        )

        blocks = build_slack_blocks(
            message="Result:",
            items_with_artwork=[(item, None)],
        )

        item_block = blocks[1]
        assert "Unknown Artist" in item_block["text"]["text"]


class TestBuildSimpleSlackBlocks:
    """Tests for build_simple_slack_blocks function."""

    def test_builds_simple_blocks_message_only(self):
        """Test building simple blocks with just a message."""
        blocks = build_simple_slack_blocks(message="Thanks for listening!")

        assert len(blocks) == 1
        assert blocks[0]["type"] == "section"
        assert "*Thanks for listening!*" in blocks[0]["text"]["text"]

    def test_builds_simple_blocks_with_context(self):
        """Test building simple blocks with context."""
        blocks = build_simple_slack_blocks(
            message="No results found", context="Try a different search term"
        )

        assert len(blocks) == 2
        assert blocks[0]["type"] == "section"
        assert blocks[1]["type"] == "context"
        assert "Try a different search term" in blocks[1]["elements"][0]["text"]
