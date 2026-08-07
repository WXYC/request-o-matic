"""Unit tests for services/slack.py."""

import json

import pytest

from models import ReleaseMetadata
from services.slack import (
    SLACK_METADATA_EVENT_TYPE,
    build_simple_slack_blocks,
    build_slack_blocks,
    build_slack_metadata,
)
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


class TestLocationUnionFoldedIntoResults:
    """Regression guard for the reverted separate-field location design
    (request-o-matic#199, reverted per the location-union-transparent plan).

    Alternate WXYC shelf locations for a track are no longer a bespoke separate
    collection with its own Slack section -- LML now folds them directly into
    the ordinary ``results`` array, so from this module's perspective they are
    just more ``items_with_artwork`` entries. This test pins that an extra,
    physical-shelf-only row (no streaming links -- exactly what a folded
    compilation-track location looks like, per the plan's honest "streaming URLs
    stay null" design) renders through the normal per-item loop, and that no
    separate location section is ever emitted.
    """

    def test_extra_result_row_renders_through_normal_loop_with_no_separate_section(
        self, sample_library_item
    ):
        # A second row shaped like a folded compilation-track location: a real
        # shelf item, no artwork/streaming metadata (LML's lean, physical-only
        # union entries carry no live URL resolution).
        location_item = make_library_item(
            id=60654,
            artist="Soundtracks - L",
            title="Lost in Translation",
            call_letters="L",
            release_call_number=654,
        )

        blocks = build_slack_blocks(
            message="Here's what I found:",
            items_with_artwork=[(sample_library_item, None), (location_item, None)],
        )

        # Header + 2 ordinary item blocks -- nothing else appended.
        assert len(blocks) == 3
        rendered = json.dumps(blocks)
        assert "Also available on" not in rendered
        # Both rows rendered through the same per-item loop, with equal standing.
        assert "Stereolab" in rendered
        assert "Soundtracks - L" in rendered
        assert "Lost in Translation" in rendered

    def test_build_slack_blocks_rejects_the_removed_locations_kwarg(self, sample_library_item):
        """The separate-field parameter is fully removed, not merely unused."""
        with pytest.raises(TypeError):
            build_slack_blocks(
                message="Here's what I found:",
                items_with_artwork=[(sample_library_item, None)],
                also_available_on=[],  # type: ignore[call-arg]
            )


class TestBuildSlackMetadata:
    """Tests for build_slack_metadata (request-o-matic#209)."""

    def test_returns_none_without_fingerprint(self):
        """Absent fingerprints attach nothing -- callers must be able to omit
        the ``metadata`` key entirely rather than sending an empty/null one."""
        assert build_slack_metadata(None) is None

    @pytest.mark.parametrize(
        "fingerprint",
        [
            pytest.param("", id="empty"),
            pytest.param("   ", id="whitespace-only"),
            pytest.param("not-a-uuid", id="malformed"),
            pytest.param("abc?inject=evil", id="query-injection"),
            pytest.param("x" * 5000, id="oversized"),
        ],
    )
    def test_returns_none_for_unusable_fingerprint(self, fingerprint):
        """A present-but-unusable header must be indistinguishable from an
        absent one. FastAPI binds an empty header to ``""`` (not None), and
        ``POST /admin/bans`` types the ban field as ``UUID`` -- so attaching a
        non-UUID here would render request-o-matic#152's "Ban requester" button
        on a value the ban endpoint rejects with 422. Bounding the value also
        keeps a caller-controlled string out of ``chat.postMessage``, whose
        ``metadata_too_large`` / ``invalid_metadata_format`` errors surface as
        a 502 that loses the listener's request entirely."""
        assert build_slack_metadata(fingerprint) is None

    def test_returns_envelope_with_fingerprint(self):
        fingerprint = "11111111-1111-4111-8111-111111111111"
        assert build_slack_metadata(fingerprint) == {
            "event_type": SLACK_METADATA_EVENT_TYPE,
            "event_payload": {"fingerprint": fingerprint},
        }

    def test_fingerprint_never_appears_in_rendered_blocks(self, sample_library_item):
        """The metadata envelope is a separate chat.postMessage parameter, not a
        block -- build_slack_blocks has no way to receive or leak it."""
        fingerprint = "11111111-1111-4111-8111-111111111111"
        metadata = build_slack_metadata(fingerprint)
        blocks = build_slack_blocks(
            message="Here's what I found:",
            items_with_artwork=[(sample_library_item, None)],
        )

        assert fingerprint not in json.dumps(blocks)
        assert fingerprint in json.dumps(metadata)


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
