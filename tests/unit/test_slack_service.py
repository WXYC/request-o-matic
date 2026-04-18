"""Unit tests for services/slack.py."""

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from models import LibraryItem, ReleaseMetadata
from services.slack import (
    build_simple_slack_blocks,
    build_slack_blocks,
    init_slack_service,
    post_to_slack,
    shutdown_slack_service,
)


@pytest.fixture
def sample_library_item():
    """Create a sample library item."""
    return LibraryItem(
        id=1,
        artist="Queen",
        title="A Night at the Opera",
        call_letters="Q",
        artist_call_number=1,
        release_call_number=1,
        genre="Rock",
        format="CD",
    )


@pytest.fixture
def sample_discogs_result():
    """Create a sample Discogs search result."""
    return ReleaseMetadata(
        artist="Queen",
        album="A Night at the Opera",
        artwork_url="https://example.com/artwork.jpg",
        release_id=12345,
        release_url="https://www.discogs.com/release/12345",
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
        assert "Queen" in item_block["text"]["text"]
        assert "A Night at the Opera" in item_block["text"]["text"]

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
        item2 = LibraryItem(
            id=2,
            artist="Queen",
            title="The Game",
            call_letters="Q",
            artist_call_number=1,
            release_call_number=2,
            genre="Rock",
            format="CD",
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
        artwork = ReleaseMetadata(
            artist="Stereolab",
            album="Aluminum Tunes",
            artwork_url="https://example.com/artwork.jpg",
            release_id=99999,
            release_url="https://www.discogs.com/release/99999",
            **{streaming_field: "https://example.com/stream"},
        )

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
        item = LibraryItem(
            id=1,
            artist=None,
            title="Unknown Album",
            call_letters="X",
            artist_call_number=1,
            release_call_number=1,
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


class TestInitSlackService:
    """Tests for init_slack_service function."""

    @pytest.mark.asyncio
    async def test_init_with_webhook_url_from_settings(self):
        """Test init uses webhook URL from settings when provided."""
        mock_settings = Mock()
        mock_settings.slack_webhook_url = "https://hooks.slack.com/services/test"

        with patch("services.slack.get_settings", return_value=mock_settings):
            with patch("services.slack.httpx.AsyncClient") as mock_client_class:
                mock_client = AsyncMock()
                mock_client_class.return_value = mock_client

                await init_slack_service()

                # Should not try to fetch webhook URL
                mock_client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_init_fetches_webhook_key(self):
        """Test init fetches webhook key when URL not in settings."""
        mock_settings = Mock()
        mock_settings.slack_webhook_url = None
        mock_settings.slack_webhook_key_url = "https://example.com/webhook-key"

        with patch("services.slack.get_settings", return_value=mock_settings):
            with patch("services.slack.httpx.AsyncClient") as mock_client_class:
                mock_client = AsyncMock()
                mock_response = Mock()
                mock_response.text = "ABC/123/XYZ"
                mock_response.raise_for_status = Mock()
                mock_client.get.return_value = mock_response
                mock_client_class.return_value = mock_client

                await init_slack_service()

                mock_client.get.assert_called_once_with("https://example.com/webhook-key")

    @pytest.mark.asyncio
    async def test_init_raises_on_fetch_failure(self):
        """Test init raises RuntimeError when webhook key fetch fails."""
        mock_settings = Mock()
        mock_settings.slack_webhook_url = None
        mock_settings.slack_webhook_key_url = "https://example.com/webhook-key"

        with patch("services.slack.get_settings", return_value=mock_settings):
            with patch("services.slack.httpx.AsyncClient") as mock_client_class:
                mock_client = AsyncMock()
                mock_client.get.side_effect = httpx.HTTPError("Connection failed")
                mock_client_class.return_value = mock_client

                with pytest.raises(RuntimeError, match="Failed to fetch Slack webhook key"):
                    await init_slack_service()


class TestShutdownSlackService:
    """Tests for shutdown_slack_service function."""

    @pytest.mark.asyncio
    async def test_shutdown_closes_client(self):
        """Test shutdown closes HTTP client."""
        import services.slack as slack_module

        mock_client = AsyncMock()
        slack_module._http_client = mock_client
        slack_module._webhook_url = "https://hooks.slack.com/test"

        await shutdown_slack_service()

        mock_client.aclose.assert_called_once()
        assert slack_module._http_client is None
        assert slack_module._webhook_url is None

    @pytest.mark.asyncio
    async def test_shutdown_handles_no_client(self):
        """Test shutdown handles case when client is None."""
        import services.slack as slack_module

        slack_module._http_client = None
        slack_module._webhook_url = None

        # Should not raise
        await shutdown_slack_service()


class TestPostToSlack:
    """Tests for post_to_slack function."""

    @pytest.mark.asyncio
    async def test_post_success(self):
        """Test successful post to Slack."""
        import services.slack as slack_module

        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_client.post.return_value = mock_response

        slack_module._http_client = mock_client
        slack_module._webhook_url = "https://hooks.slack.com/test"

        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "Test"}}]
        await post_to_slack(blocks)

        mock_client.post.assert_called_once_with(
            "https://hooks.slack.com/test", json={"blocks": blocks}
        )

    @pytest.mark.asyncio
    async def test_post_raises_when_not_configured(self):
        """Test post raises RuntimeError when service not initialized."""
        import services.slack as slack_module

        slack_module._http_client = None
        slack_module._webhook_url = None

        with pytest.raises(RuntimeError, match="Slack webhook not configured"):
            await post_to_slack([{"type": "section"}])

    @pytest.mark.asyncio
    async def test_post_raises_on_http_error(self):
        """Test post raises on HTTP error."""
        import services.slack as slack_module

        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Error", request=Mock(), response=Mock()
        )
        mock_client.post.return_value = mock_response

        slack_module._http_client = mock_client
        slack_module._webhook_url = "https://hooks.slack.com/test"

        with pytest.raises(httpx.HTTPStatusError):
            await post_to_slack([{"type": "section"}])
