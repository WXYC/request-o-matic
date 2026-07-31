"""Unit tests for services/slack.py."""

import pytest

from generated.api_models import LibraryLocation
from models import ReleaseMetadata
from services.slack import build_simple_slack_blocks, build_slack_blocks
from tests.factories import make_library_item, make_release_metadata


def _location(library_id, album_title, artist, track_position=None):
    """Build a LibraryLocation the way LML returns it in also_available_on."""
    return LibraryLocation(
        library_id=library_id,
        album_title=album_title,
        artist=artist,
        track_position=track_position,
        track_title="tommib",
        track_artist="Squarepusher",
    )


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

    def test_rowless_external_item_omits_broken_wxyc_link(self):
        """A row-less external result (LML#631) carries id=0 and an empty
        ``library_url`` — there is no WXYC catalog page for it. The Discogs link
        must still surface, but the WXYC link must be omitted rather than emitted
        as a malformed ``<|WXYC>`` (empty-target Slack link)."""
        item = make_library_item(id=0, call_number="(external)", library_url="")
        artwork = make_release_metadata(
            release_id=99999,
            release_url="https://www.discogs.com/release/99999",
            artwork_url="https://example.com/artwork.jpg",
        )

        blocks = build_slack_blocks(
            message="Result:",
            items_with_artwork=[(item, artwork)],
        )

        text = blocks[1]["text"]["text"]
        assert "<https://www.discogs.com/release/99999|Discogs>" in text
        # No WXYC link at all — and certainly not the malformed empty-target form.
        assert "WXYC" not in text
        assert "<|WXYC>" not in text

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


class TestAlsoAvailableOn:
    """Tests for rendering LookupResponse.also_available_on (ROM#199)."""

    def test_absent_locations_are_byte_identical(self, sample_library_item):
        """No also_available_on (None, [], or omitted) leaves the blocks unchanged."""
        baseline = build_slack_blocks(
            message="Here's what I found:",
            items_with_artwork=[(sample_library_item, None)],
        )
        assert (
            build_slack_blocks(
                message="Here's what I found:",
                items_with_artwork=[(sample_library_item, None)],
                also_available_on=None,
            )
            == baseline
        )
        assert (
            build_slack_blocks(
                message="Here's what I found:",
                items_with_artwork=[(sample_library_item, None)],
                also_available_on=[],
            )
            == baseline
        )

    def test_renders_ranked_locations_in_order(self, sample_library_item):
        """A populated also_available_on appends one section listing each location."""
        locations = [
            _location(60654, "Lost in Translation", "Soundtracks - L", track_position="A1"),
            _location(70001, "Go Plastic", "Squarepusher"),
        ]
        blocks = build_slack_blocks(
            message="Here's what I found:",
            items_with_artwork=[(sample_library_item, None)],
            also_available_on=locations,
        )
        # An extra block beyond the header + single item.
        assert len(blocks) == 3
        extra = blocks[-1]
        assert extra["type"] == "section"
        text = extra["text"]["text"]
        # Both releases are named, and the ranked order is preserved.
        assert "Lost in Translation" in text
        assert "Go Plastic" in text
        assert text.index("Lost in Translation") < text.index("Go Plastic")
        # Each entry links to the dj.wxyc.org shelf permalink built from library_id.
        assert "https://dj.wxyc.org/dashboard/album/legacy/60654" in text
        assert "https://dj.wxyc.org/dashboard/album/legacy/70001" in text
        # The shelf artist and position are surfaced for the DJ pulling the copy.
        assert "Soundtracks - L" in text
        assert "A1" in text

    def test_caps_long_lists_with_overflow_note(self, sample_library_item):
        """A pathologically long union is capped so the Slack section stays lean."""
        locations = [_location(1000 + i, f"Compilation {i}", "Various Artists") for i in range(25)]
        blocks = build_slack_blocks(
            message="Here's what I found:",
            items_with_artwork=[(sample_library_item, None)],
            also_available_on=locations,
        )
        text = blocks[-1]["text"]["text"]
        assert "more" in text.lower()


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
