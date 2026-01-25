"""Unit tests for core/dependencies.py."""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch, MagicMock

import httpx

from config.settings import Settings
from core.dependencies import (
    close_discogs_service,
    close_http_client,
    close_library_db,
    flush_posthog,
    get_discogs_service,
    get_groq_client,
    get_http_client,
    get_library_db,
    get_posthog_client,
    get_slack_service,
    get_slack_webhook_url,
    shutdown_posthog,
    SlackService,
)
from core.exceptions import ServiceInitializationError


@pytest.fixture
def mock_settings():
    """Create mock settings."""
    return Settings(
        groq_api_key="test_groq_key",
        discogs_token="test_discogs_token",
        slack_webhook_url="https://hooks.slack.com/test",
        library_db_path=Path("test_library.db"),
        enable_slack_integration=True,
        enable_artwork_lookup=True,
        enable_telemetry=True,
        posthog_api_key="test_posthog_key",
    )


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset module-level state before and after each test."""
    import core.dependencies as deps

    # Save original state
    original_http_client = deps._http_client
    original_library_db = deps._library_db
    original_discogs_service = deps._discogs_service
    original_posthog_client = deps._posthog_client

    # Reset state
    deps._http_client = None
    deps._library_db = None
    deps._discogs_service = None
    deps._posthog_client = None

    yield

    # Restore original state
    deps._http_client = original_http_client
    deps._library_db = original_library_db
    deps._discogs_service = original_discogs_service
    deps._posthog_client = original_posthog_client


class TestGetHttpClient:
    """Tests for get_http_client function."""

    @pytest.mark.asyncio
    async def test_creates_client(self):
        """Test that get_http_client creates a new client."""
        client = await get_http_client()
        assert isinstance(client, httpx.AsyncClient)
        await client.aclose()

    @pytest.mark.asyncio
    async def test_returns_same_instance(self):
        """Test that get_http_client returns same instance on multiple calls."""
        client1 = await get_http_client()
        client2 = await get_http_client()
        assert client1 is client2
        await client1.aclose()


class TestCloseHttpClient:
    """Tests for close_http_client function."""

    @pytest.mark.asyncio
    async def test_closes_client(self):
        """Test that close_http_client closes the client."""
        import core.dependencies as deps

        mock_client = AsyncMock()
        deps._http_client = mock_client

        await close_http_client()

        mock_client.aclose.assert_called_once()
        assert deps._http_client is None

    @pytest.mark.asyncio
    async def test_handles_no_client(self):
        """Test that close_http_client handles None client."""
        import core.dependencies as deps

        deps._http_client = None
        await close_http_client()  # Should not raise


class TestGetGroqClient:
    """Tests for get_groq_client function."""

    def test_creates_client_with_api_key(self, mock_settings):
        """Test that get_groq_client creates client when API key is set."""
        with patch("core.dependencies.Groq") as mock_groq:
            client = get_groq_client(mock_settings)
            mock_groq.assert_called_once_with(api_key="test_groq_key")

    def test_raises_without_api_key(self):
        """Test that get_groq_client raises when API key is missing."""
        settings = Settings(groq_api_key="")

        with pytest.raises(ServiceInitializationError, match="GROQ_API_KEY not configured"):
            get_groq_client(settings)


class TestGetLibraryDb:
    """Tests for get_library_db function."""

    @pytest.mark.asyncio
    async def test_creates_and_connects_db(self, mock_settings):
        """Test that get_library_db creates and connects database."""
        with patch("core.dependencies.LibraryDB") as mock_db_class:
            mock_db = AsyncMock()
            mock_db_class.return_value = mock_db

            db = await get_library_db(mock_settings)

            mock_db_class.assert_called_once()
            mock_db.connect.assert_called_once()
            assert db is mock_db

    @pytest.mark.asyncio
    async def test_returns_same_instance(self, mock_settings):
        """Test that get_library_db returns same instance."""
        import core.dependencies as deps

        mock_db = AsyncMock()
        deps._library_db = mock_db

        db = await get_library_db(mock_settings)
        assert db is mock_db

    @pytest.mark.asyncio
    async def test_raises_on_connection_failure(self, mock_settings):
        """Test that get_library_db raises on connection failure."""
        with patch("core.dependencies.LibraryDB") as mock_db_class:
            mock_db = AsyncMock()
            mock_db.connect.side_effect = Exception("Connection failed")
            mock_db_class.return_value = mock_db

            with pytest.raises(ServiceInitializationError, match="Database initialization failed"):
                await get_library_db(mock_settings)


class TestCloseLibraryDb:
    """Tests for close_library_db function."""

    @pytest.mark.asyncio
    async def test_closes_db(self):
        """Test that close_library_db closes the database."""
        import core.dependencies as deps

        mock_db = AsyncMock()
        deps._library_db = mock_db

        await close_library_db()

        mock_db.close.assert_called_once()
        assert deps._library_db is None

    @pytest.mark.asyncio
    async def test_handles_no_db(self):
        """Test that close_library_db handles None database."""
        import core.dependencies as deps

        deps._library_db = None
        await close_library_db()  # Should not raise


class TestGetDiscogsService:
    """Tests for get_discogs_service function."""

    @pytest.mark.asyncio
    async def test_creates_service_with_token(self, mock_settings):
        """Test that get_discogs_service creates service when token is set."""
        with patch("core.dependencies.DiscogsService") as mock_service_class:
            mock_service = Mock()
            mock_service_class.return_value = mock_service

            service = await get_discogs_service(mock_settings)

            mock_service_class.assert_called_once_with("test_discogs_token")
            assert service is mock_service

    @pytest.mark.asyncio
    async def test_returns_none_without_token(self):
        """Test that get_discogs_service returns None when token is missing."""
        settings = Settings(
            groq_api_key="test_key",
            discogs_token=None,
        )

        service = await get_discogs_service(settings)
        assert service is None

    @pytest.mark.asyncio
    async def test_returns_same_instance(self, mock_settings):
        """Test that get_discogs_service returns same instance."""
        import core.dependencies as deps

        mock_service = Mock()
        deps._discogs_service = mock_service

        service = await get_discogs_service(mock_settings)
        assert service is mock_service


class TestCloseDiscogsService:
    """Tests for close_discogs_service function."""

    @pytest.mark.asyncio
    async def test_closes_service(self):
        """Test that close_discogs_service closes the service."""
        import core.dependencies as deps

        mock_service = AsyncMock()
        deps._discogs_service = mock_service

        await close_discogs_service()

        mock_service.close.assert_called_once()
        assert deps._discogs_service is None

    @pytest.mark.asyncio
    async def test_handles_no_service(self):
        """Test that close_discogs_service handles None service."""
        import core.dependencies as deps

        deps._discogs_service = None
        await close_discogs_service()  # Should not raise


class TestGetPosthogClient:
    """Tests for get_posthog_client function."""

    def test_creates_client_when_enabled(self, mock_settings):
        """Test that get_posthog_client creates client when enabled."""
        with patch("core.dependencies.Posthog") as mock_posthog:
            mock_client = Mock()
            mock_posthog.return_value = mock_client

            client = get_posthog_client(mock_settings)

            mock_posthog.assert_called_once()
            assert client is mock_client

    def test_returns_none_when_disabled(self):
        """Test that get_posthog_client returns None when telemetry disabled."""
        settings = Settings(
            groq_api_key="test_key",
            enable_telemetry=False,
        )

        client = get_posthog_client(settings)
        assert client is None

    def test_returns_none_without_api_key(self):
        """Test that get_posthog_client returns None without API key."""
        settings = Settings(
            groq_api_key="test_key",
            enable_telemetry=True,
            posthog_api_key=None,
        )

        client = get_posthog_client(settings)
        assert client is None


class TestFlushPosthog:
    """Tests for flush_posthog function."""

    def test_flushes_client(self):
        """Test that flush_posthog calls flush on client."""
        import core.dependencies as deps

        mock_client = Mock()
        deps._posthog_client = mock_client

        flush_posthog()

        mock_client.flush.assert_called_once()

    def test_handles_no_client(self):
        """Test that flush_posthog handles None client."""
        import core.dependencies as deps

        deps._posthog_client = None
        flush_posthog()  # Should not raise


class TestShutdownPosthog:
    """Tests for shutdown_posthog function."""

    def test_shuts_down_client(self):
        """Test that shutdown_posthog calls shutdown on client."""
        import core.dependencies as deps

        mock_client = Mock()
        deps._posthog_client = mock_client

        shutdown_posthog()

        mock_client.shutdown.assert_called_once()
        assert deps._posthog_client is None

    def test_handles_no_client(self):
        """Test that shutdown_posthog handles None client."""
        import core.dependencies as deps

        deps._posthog_client = None
        shutdown_posthog()  # Should not raise


class TestGetSlackWebhookUrl:
    """Tests for get_slack_webhook_url function."""

    @pytest.mark.asyncio
    async def test_returns_none_when_disabled(self):
        """Test that get_slack_webhook_url returns None when disabled."""
        settings = Settings(
            groq_api_key="test_key",
            enable_slack_integration=False,
        )
        mock_client = AsyncMock()

        url = await get_slack_webhook_url(settings, mock_client)
        assert url is None

    @pytest.mark.asyncio
    async def test_returns_url_from_settings(self, mock_settings):
        """Test that get_slack_webhook_url returns URL from settings."""
        mock_client = AsyncMock()

        url = await get_slack_webhook_url(mock_settings, mock_client)
        assert url == "https://hooks.slack.com/test"

    @pytest.mark.asyncio
    async def test_fetches_url_from_railway(self):
        """Test that get_slack_webhook_url fetches URL from Railway endpoint."""
        settings = Settings(
            groq_api_key="test_key",
            enable_slack_integration=True,
            slack_webhook_url=None,
        )
        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.text = "ABC/123/XYZ"
        mock_response.raise_for_status = Mock()
        mock_client.get.return_value = mock_response

        url = await get_slack_webhook_url(settings, mock_client)

        assert url == "https://hooks.slack.com/services/ABC/123/XYZ"

    @pytest.mark.asyncio
    async def test_raises_on_fetch_failure(self):
        """Test that get_slack_webhook_url raises on fetch failure."""
        settings = Settings(
            groq_api_key="test_key",
            enable_slack_integration=True,
            slack_webhook_url=None,
        )
        mock_client = AsyncMock()
        mock_client.get.side_effect = Exception("Connection failed")

        with pytest.raises(ServiceInitializationError, match="Failed to fetch Slack webhook key"):
            await get_slack_webhook_url(settings, mock_client)


class TestSlackService:
    """Tests for SlackService class."""

    @pytest.mark.asyncio
    async def test_post_blocks(self):
        """Test that SlackService posts blocks to webhook."""
        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_client.post.return_value = mock_response

        service = SlackService(
            webhook_url="https://hooks.slack.com/test",
            http_client=mock_client,
        )

        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "Test"}}]
        await service.post_blocks(blocks)

        mock_client.post.assert_called_once_with(
            "https://hooks.slack.com/test", json={"blocks": blocks}
        )

    @pytest.mark.asyncio
    async def test_post_blocks_raises_on_error(self):
        """Test that SlackService raises on HTTP error."""
        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Error", request=Mock(), response=Mock()
        )
        mock_client.post.return_value = mock_response

        service = SlackService(
            webhook_url="https://hooks.slack.com/test",
            http_client=mock_client,
        )

        with pytest.raises(httpx.HTTPStatusError):
            await service.post_blocks([{"type": "section"}])


class TestGetSlackService:
    """Tests for get_slack_service function."""

    @pytest.mark.asyncio
    async def test_returns_none_when_no_webhook(self):
        """Test that get_slack_service returns None without webhook URL."""
        mock_client = AsyncMock()

        service = await get_slack_service(webhook_url=None, http_client=mock_client)
        assert service is None

    @pytest.mark.asyncio
    async def test_returns_service_with_webhook(self):
        """Test that get_slack_service returns service with webhook URL."""
        mock_client = AsyncMock()

        service = await get_slack_service(
            webhook_url="https://hooks.slack.com/test",
            http_client=mock_client,
        )

        assert isinstance(service, SlackService)
        assert service.webhook_url == "https://hooks.slack.com/test"
        assert service.http_client is mock_client
