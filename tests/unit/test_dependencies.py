"""Unit tests for core/dependencies.py."""

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from config.settings import Settings
from core.dependencies import (
    SlackService,
    close_http_client,
    flush_posthog,
    get_groq_client,
    get_http_client,
    get_posthog_client,
    get_slack_service,
    get_slack_webhook_url,
    shutdown_posthog,
)
from core.exceptions import ServiceInitializationError


@pytest.fixture
def mock_settings():
    """Create mock settings."""
    return Settings(
        groq_api_key="test_groq_key",
        slack_webhook_url="https://hooks.slack.com/test",
        enable_slack_integration=True,
        enable_telemetry=True,
        posthog_api_key="test_posthog_key",
    )


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset module-level state before and after each test."""
    import core.dependencies as deps

    # Save original state
    original_http_client = deps._http_client
    original_posthog_client = deps._posthog_client
    original_slack_webhook_url = deps._slack_webhook_url
    original_warned_missing_posthog_key = deps._warned_missing_posthog_key

    # Reset state
    deps._http_client = None
    deps._posthog_client = None
    deps._slack_webhook_url = None
    deps._warned_missing_posthog_key = False

    yield

    # Restore original state
    deps._http_client = original_http_client
    deps._posthog_client = original_posthog_client
    deps._slack_webhook_url = original_slack_webhook_url
    deps._warned_missing_posthog_key = original_warned_missing_posthog_key


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
        with patch("core.dependencies.AsyncGroq") as mock_groq:
            get_groq_client(mock_settings)
            mock_groq.assert_called_once_with(api_key="test_groq_key", max_retries=4)

    def test_raises_without_api_key(self):
        """Test that get_groq_client raises when API key is missing."""
        settings = Settings(groq_api_key="")

        with pytest.raises(ServiceInitializationError, match="GROQ_API_KEY not configured"):
            get_groq_client(settings)


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

    def test_logs_warning_when_telemetry_enabled_but_key_missing(self, caplog):
        """When the operator wanted telemetry (enable_telemetry=True) but the key
        is missing, that's a misconfiguration — log at WARNING so it shows up in
        normal log scraping. PostHog telemetry has been silently dead in
        production for ~2.5 months because this was DEBUG. (#111)"""
        settings = Settings(
            groq_api_key="test_key",
            enable_telemetry=True,
            posthog_api_key=None,
        )

        caplog.clear()
        with caplog.at_level("WARNING", logger="core.dependencies"):
            client = get_posthog_client(settings)

        assert client is None
        assert any(
            "POSTHOG_API_KEY" in record.message and record.levelname == "WARNING"
            for record in caplog.records
        ), (
            f"expected a WARNING about POSTHOG_API_KEY, got: {[(r.levelname, r.message) for r in caplog.records]}"
        )

    def test_warning_is_one_shot_per_process(self, caplog):
        """``get_posthog_client`` is a per-request FastAPI dependency; if the
        WARN fired on every call it would flood the log stream. Subsequent
        calls within the same process must stay silent until the module-level
        ``_warned_missing_posthog_key`` flag is reset (e.g. by a process
        restart). (#111 review feedback)"""
        settings = Settings(
            groq_api_key="test_key",
            enable_telemetry=True,
            posthog_api_key=None,
        )

        caplog.clear()
        with caplog.at_level("WARNING", logger="core.dependencies"):
            for _ in range(5):
                assert get_posthog_client(settings) is None

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1, (
            f"expected exactly one WARNING across 5 calls, got {len(warnings)}: "
            f"{[r.message for r in warnings]}"
        )

    def test_logs_debug_when_telemetry_explicitly_disabled(self, caplog):
        """When telemetry is explicitly disabled, that's the operator's intent
        — keep the log at DEBUG to avoid noise."""
        settings = Settings(
            groq_api_key="test_key",
            enable_telemetry=False,
        )

        caplog.clear()
        with caplog.at_level("DEBUG", logger="core.dependencies"):
            client = get_posthog_client(settings)

        assert client is None
        # No WARNING-level records should have been emitted by this path.
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings == [], f"unexpected warnings: {[r.message for r in warnings]}"


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
