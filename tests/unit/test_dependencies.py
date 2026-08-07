"""Unit tests for core/dependencies.py."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastapi import HTTPException

from config.settings import Settings
from core.dependencies import (
    SlackService,
    close_http_client,
    get_ban_admin_client,
    get_groq_client,
    get_http_client,
    get_posthog_client,
    get_slack_bot_config,
    get_slack_service,
    get_slack_webhook_url,
    require_admin_token,
)
from core.exceptions import ServiceInitializationError, SlackPostError
from services.ban_admin_client import BanAdminClient


@pytest.fixture
def mock_settings():
    """Create mock settings."""
    return Settings(
        groq_api_key="test_groq_key",
        slack_webhook_url="https://hooks.slack.com/test",
        enable_slack_integration=True,
        # Pinned, not inherited: Settings reads `.env` and the process
        # environment, so leaving this unset makes every webhook-path test
        # below depend on whether the developer happens to have
        # SLACK_USE_BOT_TOKEN set -- which README tells them to set when
        # exercising the migration (#215).
        slack_use_bot_token=False,
        enable_telemetry=True,
        posthog_api_key="test_posthog_key",
    )


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset module-level state before and after each test.

    The HTTP client singleton lives inside the closure returned by
    ``async_singleton``; we reset it via its public closer (which clears the
    cached instance and invokes ``aclose`` if one was built). The Slack webhook
    URL remains a plain module global, reset directly.
    """
    import core.dependencies as deps

    async def _close() -> None:
        await deps.close_http_client()

    original_slack_webhook_url = deps._slack_webhook_url

    asyncio.run(_close())
    deps._slack_webhook_url = None

    yield

    asyncio.run(_close())
    deps._slack_webhook_url = original_slack_webhook_url


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

    @pytest.mark.asyncio
    async def test_get_http_client_factory_invoked_once_under_concurrency(self):
        """Concurrent first-callers must see exactly one underlying httpx client.

        Port of the LML#241 / LML#242 FD-leak reproducer adapted to rom. The
        rom-side wiring is ``get_http_client = async_singleton(_make_http_client)[0]``;
        we exercise the same race against a freshly-built ``async_singleton``
        wrapping ``_make_http_client`` so the test is independent of any cached
        module state and works regardless of whether other tests have already
        warmed the rom-level singleton.

        Without the ``asyncio.Lock`` inside ``async_singleton``, concurrent
        first-callers each pass the outer ``is None`` check, each invoke the
        factory, and only one survives as the cached value — the rest are
        orphaned with their connections (and FDs) still open. With the lock,
        the factory runs exactly once.
        """
        from wxyc_fastapi.http import async_singleton

        from core.dependencies import _make_http_client

        invocations = 0

        async def counting_factory() -> httpx.AsyncClient:
            nonlocal invocations
            invocations += 1
            await asyncio.sleep(0)  # yield, let other concurrent callers race
            return await _make_http_client()

        getter, closer = async_singleton(counting_factory)
        try:
            results = await asyncio.gather(*(getter() for _ in range(50)))
            assert invocations == 1, (
                f"Expected exactly one factory invocation under concurrent "
                f"first-callers, saw {invocations}. This is the LML#241 FD-leak "
                f"race — get_http_client needs an asyncio.Lock guard (via "
                f"wxyc_fastapi.http.async_singleton)."
            )
            assert all(r is results[0] for r in results)
        finally:
            await closer()

    @pytest.mark.asyncio
    async def test_get_http_client_is_wired_via_async_singleton(self):
        """Pin the wiring: rom's ``get_http_client`` and ``close_http_client``
        must be the (getter, closer) pair returned by ``async_singleton``.

        This catches a future regression where someone reverts to a hand-rolled
        ``global _http_client`` pattern without the lock — the race-regression
        test above would still pass (it tests the helper in isolation), but
        rom would silently lose the FD-leak guard.
        """
        from wxyc_fastapi.http.singleton import async_singleton

        import core.dependencies as deps

        # The helper returns local closures defined in
        # ``wxyc_fastapi.http.singleton`` with qualnames
        # ``async_singleton.<locals>.getter`` / ``...closer``; both invariants
        # together pin rom's getter/closer to this exact helper.
        probe_getter, probe_closer = async_singleton(deps._make_http_client)
        try:
            assert deps.get_http_client.__module__ == probe_getter.__module__
            assert deps.get_http_client.__qualname__ == probe_getter.__qualname__
            assert deps.close_http_client.__module__ == probe_closer.__module__
            assert deps.close_http_client.__qualname__ == probe_closer.__qualname__
        finally:
            await probe_closer()


class TestCloseHttpClient:
    """Tests for close_http_client function."""

    @pytest.mark.asyncio
    async def test_closes_client(self):
        """Test that close_http_client closes the client and clears the singleton.

        After ``close_http_client()`` returns, the next ``get_http_client()``
        call must build a fresh client via the factory — not return the
        torn-down one. This is the closer-resets-state contract from
        ``async_singleton``.
        """
        first = await get_http_client()
        assert isinstance(first, httpx.AsyncClient)
        assert first.is_closed is False

        await close_http_client()
        assert first.is_closed is True

        second = await get_http_client()
        assert second is not first
        assert second.is_closed is False
        await close_http_client()

    @pytest.mark.asyncio
    async def test_handles_no_client(self):
        """Test that close_http_client is a no-op when never initialized."""
        # The autouse fixture has already reset state; closer-before-getter
        # must not raise.
        await close_http_client()


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
    """Tests for the rom-side gating of get_posthog_client.

    Underlying client construction, the warn-once-per-prefix semantics, and the
    no-API-key path are exercised in the wxyc-fastapi test suite. These tests
    pin only what's specific to rom: the `enable_telemetry` short-circuit and
    that we delegate with the right `event_prefix`.
    """

    def test_short_circuits_when_telemetry_disabled(self):
        """`enable_telemetry=False` returns None without calling the wxyc-fastapi client."""
        settings = Settings(
            groq_api_key="test_key",
            enable_telemetry=False,
        )
        with patch("core.dependencies._shared_posthog_client") as mock_shared:
            client = get_posthog_client(settings)
        assert client is None
        mock_shared.assert_not_called()

    def test_delegates_to_wxyc_fastapi_with_request_event_prefix(self, mock_settings):
        """When enabled, delegates to wxyc-fastapi with `event_prefix="request"`."""
        with patch("core.dependencies._shared_posthog_client") as mock_shared:
            mock_shared.return_value = Mock()
            client = get_posthog_client(mock_settings)
        assert client is mock_shared.return_value
        mock_shared.assert_called_once_with(event_prefix="request")


class TestGetBanCheckClient:
    """Tests for get_ban_check_client function.

    The provider is the only line of defense that prevents BS calls when the
    feature flag is off or the URL is unset, so it has to be pinned both ways.
    """

    @pytest.mark.asyncio
    async def test_returns_none_when_flag_off(self):
        """Default config has ENFORCE_REQUEST_BANS=False; provider returns None."""
        from core.dependencies import get_ban_check_client

        settings = Settings(
            groq_api_key="test_key",
            bs_check_request_ban_url="http://bs/auth/check-request-ban",
            enforce_request_bans=False,
        )
        client = await get_ban_check_client(settings, AsyncMock())
        assert client is None

    @pytest.mark.asyncio
    async def test_returns_none_when_url_unset(self):
        """Flag on but URL missing → still None (no half-configured calls)."""
        from core.dependencies import get_ban_check_client

        settings = Settings(
            groq_api_key="test_key",
            bs_check_request_ban_url=None,
            enforce_request_bans=True,
        )
        client = await get_ban_check_client(settings, AsyncMock())
        assert client is None

    @pytest.mark.asyncio
    async def test_returns_client_when_enabled(self):
        """Flag on AND URL set → return a wired BanCheckClient."""
        from core.dependencies import get_ban_check_client
        from services.ban_check_client import BanCheckClient

        settings = Settings(
            groq_api_key="test_key",
            bs_check_request_ban_url="http://bs/auth/check-request-ban",
            enforce_request_bans=True,
        )
        http_client = AsyncMock()
        client = await get_ban_check_client(settings, http_client)
        assert isinstance(client, BanCheckClient)
        assert client.url == "http://bs/auth/check-request-ban"
        assert client.http_client is http_client


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
            slack_use_bot_token=False,
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
            slack_use_bot_token=False,
        )
        mock_client = AsyncMock()
        mock_client.get.side_effect = Exception("Connection failed")

        with pytest.raises(ServiceInitializationError, match="Failed to fetch Slack webhook key"):
            await get_slack_webhook_url(settings, mock_client)

    @pytest.mark.asyncio
    async def test_returns_none_and_skips_fetch_when_bot_token_flag_on(self):
        """SLACK_USE_BOT_TOKEN=true short-circuits before touching Railway (#215).

        Without this, an unset/unreachable SLACK_WEBHOOK_KEY_URL would raise
        ServiceInitializationError on every /request even when the bot-token
        transport is fully configured, since FastAPI resolves this dependency
        unconditionally.
        """
        settings = Settings(
            groq_api_key="test_key",
            enable_slack_integration=True,
            slack_use_bot_token=True,
            slack_webhook_url=None,
        )
        mock_client = AsyncMock()

        url = await get_slack_webhook_url(settings, mock_client)

        assert url is None
        mock_client.get.assert_not_called()


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
        result = await service.post_blocks(blocks)

        mock_client.post.assert_called_once_with(
            "https://hooks.slack.com/test", json={"blocks": blocks}
        )
        assert result is None

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


class TestSlackServiceBotToken:
    """Tests for SlackService's chat.postMessage bot-token transport (#215)."""

    @pytest.mark.asyncio
    async def test_post_blocks_bot_token_posts_and_returns_ts(self):
        """Bot-token posts hit chat.postMessage with a Bearer header and return ts."""
        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = {"ok": True, "ts": "1234.5678", "channel": "C123"}
        mock_client.post.return_value = mock_response

        service = SlackService(
            http_client=mock_client,
            bot_token="xoxb-test-token",
            channel_id="C123",
        )

        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "Test"}}]
        result = await service.post_blocks(blocks)

        mock_client.post.assert_called_once_with(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": "Bearer xoxb-test-token"},
            json={"channel": "C123", "blocks": blocks},
        )
        assert result == "1234.5678"

    @pytest.mark.asyncio
    async def test_post_blocks_bot_token_ok_false_raises(self):
        """A 200 {"ok": false} response must raise, not log success (#215)."""
        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = {"ok": False, "error": "invalid_blocks"}
        mock_client.post.return_value = mock_response

        service = SlackService(
            http_client=mock_client,
            bot_token="xoxb-test-token",
            channel_id="C123",
        )

        with pytest.raises(SlackPostError, match="invalid_blocks"):
            await service.post_blocks([{"type": "section"}])

    @pytest.mark.asyncio
    async def test_post_blocks_bot_token_not_in_channel_names_channel(self):
        """``not_in_channel`` gets its own distinguishable error naming the channel."""
        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = {"ok": False, "error": "not_in_channel"}
        mock_client.post.return_value = mock_response

        service = SlackService(
            http_client=mock_client,
            bot_token="xoxb-test-token",
            channel_id="C123",
        )

        with pytest.raises(SlackPostError, match="not_in_channel") as exc_info:
            await service.post_blocks([{"type": "section"}])
        assert "C123" in str(exc_info.value)
        # Regression: the bot token must never leak into an error message.
        assert "xoxb-" not in str(exc_info.value)


class TestGetSlackService:
    """Tests for get_slack_service function."""

    @pytest.mark.asyncio
    async def test_returns_none_when_no_webhook(self):
        """Test that get_slack_service returns None without webhook URL."""
        mock_client = AsyncMock()
        settings = Settings(groq_api_key="test_key", slack_use_bot_token=False)

        service = await get_slack_service(
            settings=settings, webhook_url=None, bot_config=None, http_client=mock_client
        )
        assert service is None

    @pytest.mark.asyncio
    async def test_returns_service_with_webhook(self):
        """Test that get_slack_service returns service with webhook URL."""
        mock_client = AsyncMock()
        settings = Settings(groq_api_key="test_key", slack_use_bot_token=False)

        service = await get_slack_service(
            settings=settings,
            webhook_url="https://hooks.slack.com/test",
            bot_config=None,
            http_client=mock_client,
        )

        assert isinstance(service, SlackService)
        assert service.webhook_url == "https://hooks.slack.com/test"
        assert service.http_client is mock_client

    @pytest.mark.asyncio
    async def test_bot_token_flag_on_returns_bot_service(self):
        """SLACK_USE_BOT_TOKEN=true routes to the bot-token transport, ignoring webhook_url."""
        mock_client = AsyncMock()
        settings = Settings(groq_api_key="test_key", slack_use_bot_token=True)

        service = await get_slack_service(
            settings=settings,
            webhook_url="https://hooks.slack.com/test",
            bot_config=("xoxb-test-token", "C123"),
            http_client=mock_client,
        )

        assert isinstance(service, SlackService)
        assert service.bot_token == "xoxb-test-token"
        assert service.channel_id == "C123"

    @pytest.mark.asyncio
    async def test_bot_token_flag_on_but_unresolved_config_returns_none(self):
        """Flag on but bot_config didn't resolve (missing token/channel) -> no service, not a webhook fallback."""
        mock_client = AsyncMock()
        settings = Settings(groq_api_key="test_key", slack_use_bot_token=True)

        service = await get_slack_service(
            settings=settings,
            webhook_url="https://hooks.slack.com/test",
            bot_config=None,
            http_client=mock_client,
        )

        assert service is None

    @pytest.mark.asyncio
    async def test_bot_token_flag_off_ignores_bot_config(self):
        """Flag off keeps the webhook path even if bot_config happens to resolve."""
        mock_client = AsyncMock()
        settings = Settings(groq_api_key="test_key", slack_use_bot_token=False)

        service = await get_slack_service(
            settings=settings,
            webhook_url="https://hooks.slack.com/test",
            bot_config=("xoxb-test-token", "C123"),
            http_client=mock_client,
        )

        assert isinstance(service, SlackService)
        assert service.webhook_url == "https://hooks.slack.com/test"
        assert service.bot_token is None


class TestGetSlackBotConfig:
    """Tests for get_slack_bot_config function."""

    @pytest.mark.asyncio
    async def test_returns_none_when_flag_off(self):
        settings = Settings(
            groq_api_key="test_key",
            slack_use_bot_token=False,
            slack_bot_token="xoxb-test-token",
            slack_channel_id="C123",
        )
        assert await get_slack_bot_config(settings) is None

    @pytest.mark.asyncio
    async def test_returns_none_when_integration_disabled(self):
        settings = Settings(
            groq_api_key="test_key",
            enable_slack_integration=False,
            slack_use_bot_token=True,
            slack_bot_token="xoxb-test-token",
            slack_channel_id="C123",
        )
        assert await get_slack_bot_config(settings) is None

    @pytest.mark.asyncio
    async def test_returns_none_when_bot_token_missing(self):
        settings = Settings(
            groq_api_key="test_key",
            slack_use_bot_token=True,
            slack_bot_token=None,
            slack_channel_id="C123",
        )
        assert await get_slack_bot_config(settings) is None

    @pytest.mark.asyncio
    async def test_returns_none_when_channel_id_missing(self):
        settings = Settings(
            groq_api_key="test_key",
            slack_use_bot_token=True,
            slack_bot_token="xoxb-test-token",
            slack_channel_id=None,
        )
        assert await get_slack_bot_config(settings) is None

    @pytest.mark.asyncio
    async def test_returns_tuple_when_fully_configured(self):
        settings = Settings(
            groq_api_key="test_key",
            slack_use_bot_token=True,
            slack_bot_token="xoxb-test-token",
            slack_channel_id="C123",
        )
        assert await get_slack_bot_config(settings) == ("xoxb-test-token", "C123")


# ---------------------------------------------------------------------------
# Admin auth + ban admin client (#151)
# ---------------------------------------------------------------------------


class TestRequireAdminToken:
    """Tests for the ``require_admin_token`` FastAPI dependency."""

    def test_passes_with_correct_bearer(self):
        settings = Settings(groq_api_key="x", admin_token="secret")
        # Should not raise
        require_admin_token(settings=settings, authorization="Bearer secret")

    def test_case_insensitive_scheme(self):
        settings = Settings(groq_api_key="x", admin_token="secret")
        require_admin_token(settings=settings, authorization="bearer secret")
        require_admin_token(settings=settings, authorization="BEARER secret")

    def test_missing_header_raises_401(self):
        settings = Settings(groq_api_key="x", admin_token="secret")
        with pytest.raises(HTTPException) as excinfo:
            require_admin_token(settings=settings, authorization=None)
        assert excinfo.value.status_code == 401

    def test_wrong_token_raises_403(self):
        settings = Settings(groq_api_key="x", admin_token="secret")
        with pytest.raises(HTTPException) as excinfo:
            require_admin_token(settings=settings, authorization="Bearer wrong")
        assert excinfo.value.status_code == 403

    def test_admin_token_unset_raises_403(self):
        """Fail-closed: server with no ADMIN_TOKEN rejects every request."""
        settings = Settings(groq_api_key="x", admin_token=None)
        with pytest.raises(HTTPException) as excinfo:
            require_admin_token(settings=settings, authorization="Bearer anything")
        assert excinfo.value.status_code == 403

    def test_admin_token_empty_string_raises_403(self):
        """Pins the empty-string fail-closed path. Operator who accidentally
        clears the Railway variable (ADMIN_TOKEN=) gets 'disabled', not 'wrong
        token' — and the empty bearer never authenticates."""
        settings = Settings(groq_api_key="x", admin_token="")
        with pytest.raises(HTTPException) as excinfo:
            require_admin_token(settings=settings, authorization="Bearer ")
        assert excinfo.value.status_code == 403

    def test_tolerates_extra_whitespace_in_header(self):
        """RFC 7235 allows 1*SP between scheme and token; copy-paste from a
        wiki or notes app often introduces double spaces or tabs. Strict
        single-space split would 403 the operator with a misleading 'Invalid
        token' message."""
        settings = Settings(groq_api_key="x", admin_token="secret")
        # Multiple spaces between scheme and token
        require_admin_token(settings=settings, authorization="Bearer  secret")
        # Tab between scheme and token
        require_admin_token(settings=settings, authorization="Bearer\tsecret")
        # Surrounding whitespace
        require_admin_token(settings=settings, authorization="  Bearer secret  ")

    def test_uses_constant_time_comparison(self):
        """Smoke check that hmac.compare_digest is being used (the comparison
        path returns False for wrong tokens of the same length AND of
        different lengths — both raise 403 rather than leaking via length-
        dependent control flow)."""
        settings = Settings(groq_api_key="x", admin_token="secret-token-123")
        # Same-length wrong token
        with pytest.raises(HTTPException):
            require_admin_token(settings=settings, authorization="Bearer XXXXXX-token-123")
        # Different-length wrong token (compare_digest doesn't short-circuit)
        with pytest.raises(HTTPException):
            require_admin_token(settings=settings, authorization="Bearer short")


class TestGetBanAdminClient:
    """Tests for the ``get_ban_admin_client`` FastAPI dependency."""

    @pytest.mark.asyncio
    async def test_builds_client_with_settings(self):
        settings = Settings(
            groq_api_key="x",
            bs_internal_bans_url="https://bs.example.com/internal/banned-fingerprints",
            bs_internal_key="key-123",
        )
        http = httpx.AsyncClient()
        try:
            client = await get_ban_admin_client(settings=settings, http_client=http)
        finally:
            await http.aclose()

        assert isinstance(client, BanAdminClient)
        assert client.base_url == "https://bs.example.com/internal/banned-fingerprints"
        assert client.internal_key == "key-123"
        assert client.http_client is http

    @pytest.mark.asyncio
    async def test_missing_url_raises_503(self):
        settings = Settings(groq_api_key="x", bs_internal_bans_url=None, bs_internal_key="key")
        http = httpx.AsyncClient()
        try:
            with pytest.raises(HTTPException) as excinfo:
                await get_ban_admin_client(settings=settings, http_client=http)
        finally:
            await http.aclose()
        assert excinfo.value.status_code == 503

    @pytest.mark.asyncio
    async def test_missing_key_raises_503(self):
        settings = Settings(
            groq_api_key="x",
            bs_internal_bans_url="https://bs.example.com/internal/banned-fingerprints",
            bs_internal_key=None,
        )
        http = httpx.AsyncClient()
        try:
            with pytest.raises(HTTPException) as excinfo:
                await get_ban_admin_client(settings=settings, http_client=http)
        finally:
            await http.aclose()
        assert excinfo.value.status_code == 503
