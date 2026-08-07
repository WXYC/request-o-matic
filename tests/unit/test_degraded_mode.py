"""Unit tests for degraded-mode fallbacks when external dependencies fail.

Goal: when Groq (parsing) or LML (search) is unavailable, the service must
still post the listener's message to Slack rather than returning an error.
Slack remains the only required dependency for a successful request.

Behavior matrix:
- Groq down: post raw message with "_Parsing unavailable_" context, no parse fields.
- LML down (or unconfigured): post parsed message with "_Search unavailable_" context.
- Both down: post raw message with "_Parsing unavailable_" context.
- Slack down: surface the error (502) since we cannot fall back further.
"""

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastapi import FastAPI
from groq import RateLimitError
from httpx import ASGITransport, AsyncClient

from core.dependencies import (
    get_groq_client,
    get_lookup_client,
    get_posthog_client,
    get_slack_service,
)
from routers.request import router
from services.lookup_client import LookupServiceClient
from tests.conftest import make_parsed_request

SAMPLE_PARSED = make_parsed_request(
    song="la paradoja",
    artist="Juana Molina",
    raw_message="play la paradoja by juana molina",
)


def _make_app(
    *,
    lookup_client: LookupServiceClient | None,
    slack_service: AsyncMock | None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_groq_client] = lambda: Mock()
    app.dependency_overrides[get_slack_service] = lambda: slack_service
    app.dependency_overrides[get_posthog_client] = lambda: None
    app.dependency_overrides[get_lookup_client] = lambda: lookup_client
    return app


@pytest.fixture
def mock_lookup_client():
    return AsyncMock(spec=LookupServiceClient)


@pytest.fixture
def mock_slack_service():
    svc = AsyncMock()
    svc.post_blocks = AsyncMock()
    svc.webhook_url = "https://hooks.slack.com/test"
    return svc


def _slack_text_at(call, index: int) -> str:
    """Extract the text from the i-th block of a captured `post_blocks` call."""
    blocks = call.args[0]
    block = blocks[index]
    if block.get("type") == "context":
        return str(block["elements"][0]["text"])
    return str(block["text"]["text"])


# -----------------------------------------------------------------------------
# LML degraded-mode tests
# -----------------------------------------------------------------------------


class TestLMLDegradedMode:
    """When LML fails, post the parsed message to Slack with a Search-unavailable note."""

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.HTTPStatusError(
                "500",
                request=httpx.Request("POST", "http://lml/lookup"),
                response=httpx.Response(500),
            ),
            httpx.ConnectError("Connection refused"),
            httpx.TimeoutException("Read timed out"),
        ],
        ids=["http_500", "connect_error", "timeout"],
    )
    @pytest.mark.asyncio
    async def test_lml_failure_returns_200_and_posts_to_slack(
        self, mock_lookup_client, mock_slack_service, exc
    ):
        mock_lookup_client.lookup.side_effect = exc
        app = _make_app(lookup_client=mock_lookup_client, slack_service=mock_slack_service)

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja by juana molina"},
                )

        assert response.status_code == 200
        data = response.json()
        assert data["degraded_mode"] == "search_unavailable"
        assert data["library_results"] == []
        assert data["search_type"] == "none"
        # Parsed metadata is preserved — Groq still worked.
        assert data["parsed"]["artist"] == "Juana Molina"
        assert data["parsed"]["song"] == "la paradoja"

        mock_slack_service.post_blocks.assert_awaited_once()
        context_text = _slack_text_at(mock_slack_service.post_blocks.call_args, 1)
        assert "Search unavailable" in context_text
        # Parsed fields surfaced in the context for the DJ
        assert "Juana Molina" in context_text

    @pytest.mark.asyncio
    async def test_lml_not_configured_returns_200_and_posts_to_slack(self, mock_slack_service):
        """Missing LOOKUP_SERVICE_URL is treated as a permanently degraded LML."""
        app = _make_app(lookup_client=None, slack_service=mock_slack_service)

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja by juana molina"},
                )

        assert response.status_code == 200
        data = response.json()
        assert data["degraded_mode"] == "search_unavailable"
        mock_slack_service.post_blocks.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lml_failure_skip_slack_does_not_post(
        self, mock_lookup_client, mock_slack_service
    ):
        """skip_slack=True still suppresses the Slack post in the degraded path."""
        mock_lookup_client.lookup.side_effect = httpx.ConnectError("boom")
        app = _make_app(lookup_client=mock_lookup_client, slack_service=mock_slack_service)

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja", "skip_slack": True},
                )

        assert response.status_code == 200
        assert response.json()["degraded_mode"] == "search_unavailable"
        mock_slack_service.post_blocks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lml_failure_for_non_request_does_not_use_search_unavailable(
        self, mock_lookup_client, mock_slack_service
    ):
        """Non-requests never call LML, so a broken LML must not change their flow."""
        from services.parser import MessageType

        parsed = make_parsed_request(
            is_request=False,
            message_type=MessageType.FEEDBACK,
            raw_message="love the show!",
        )
        # Setting side_effect would only matter if .lookup were called; verify it's not.
        mock_lookup_client.lookup.side_effect = httpx.ConnectError("boom")
        app = _make_app(lookup_client=mock_lookup_client, slack_service=mock_slack_service)

        with patch("routers.request.parse_request", new_callable=AsyncMock, return_value=parsed):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "love the show!"},
                )

        assert response.status_code == 200
        assert response.json().get("degraded_mode") is None
        mock_lookup_client.lookup.assert_not_awaited()


# -----------------------------------------------------------------------------
# Groq degraded-mode tests
# -----------------------------------------------------------------------------


class TestGroqDegradedMode:
    """When Groq fails, post the raw message with no classification."""

    @pytest.mark.parametrize(
        "exc",
        [
            RateLimitError(
                message="Rate limit exceeded",
                response=httpx.Response(429, request=httpx.Request("POST", "https://api.groq.com")),
                body=None,
            ),
            ValueError("Invalid JSON response from Groq"),
            httpx.ConnectError("Groq is unreachable"),
            RuntimeError("unexpected groq error"),
        ],
        ids=["rate_limit", "value_error", "connect_error", "runtime_error"],
    )
    @pytest.mark.asyncio
    async def test_groq_failure_returns_200_and_posts_raw_to_slack(
        self, mock_lookup_client, mock_slack_service, exc
    ):
        app = _make_app(lookup_client=mock_lookup_client, slack_service=mock_slack_service)

        with patch("routers.request.parse_request", new_callable=AsyncMock, side_effect=exc):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja by juana molina"},
                )

        assert response.status_code == 200
        data = response.json()
        assert data["degraded_mode"] == "parsing_unavailable"
        assert data["parsed"] is None
        assert data["library_results"] == []
        assert data["search_type"] == "none"

        mock_slack_service.post_blocks.assert_awaited_once()
        # Header block contains the raw listener message verbatim.
        header_text = _slack_text_at(mock_slack_service.post_blocks.call_args, 0)
        assert "play la paradoja by juana molina" in header_text
        context_text = _slack_text_at(mock_slack_service.post_blocks.call_args, 1)
        assert "Parsing unavailable" in context_text

        # We never even attempted LML when parsing failed.
        mock_lookup_client.lookup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_groq_failure_skip_slack_does_not_post(
        self, mock_lookup_client, mock_slack_service
    ):
        app = _make_app(lookup_client=mock_lookup_client, slack_service=mock_slack_service)

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            side_effect=ValueError("broken"),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja", "skip_slack": True},
                )

        assert response.status_code == 200
        assert response.json()["degraded_mode"] == "parsing_unavailable"
        mock_slack_service.post_blocks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_groq_failure_with_no_lookup_client(self, mock_slack_service):
        """Both Groq down AND LML unconfigured: still post the raw message."""
        app = _make_app(lookup_client=None, slack_service=mock_slack_service)

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            side_effect=RuntimeError("groq down"),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                )

        assert response.status_code == 200
        assert response.json()["degraded_mode"] == "parsing_unavailable"
        mock_slack_service.post_blocks.assert_awaited_once()


# -----------------------------------------------------------------------------
# Fingerprint metadata on degraded posts (request-o-matic#209)
# -----------------------------------------------------------------------------


class TestFingerprintOnDegradedPaths:
    """Degraded-mode posts carry the fingerprint metadata on the same terms as
    a normal post -- an abuser is more likely to hit degraded paths than less."""

    @pytest.mark.asyncio
    async def test_parsing_unavailable_attaches_fingerprint_metadata(
        self, mock_lookup_client, mock_slack_service
    ):
        fingerprint = "22222222-2222-4222-8222-222222222222"
        app = _make_app(lookup_client=mock_lookup_client, slack_service=mock_slack_service)

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            side_effect=ValueError("broken"),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                    headers={"X-Device-Fingerprint": fingerprint},
                )

        assert response.status_code == 200
        mock_slack_service.post_blocks.assert_awaited_once()
        call = mock_slack_service.post_blocks.call_args
        assert call.kwargs["metadata"] == {
            "event_type": "request_posted",
            "event_payload": {"fingerprint": fingerprint},
        }
        assert fingerprint not in str(call.args[0])

    @pytest.mark.asyncio
    async def test_search_unavailable_attaches_fingerprint_metadata(
        self, mock_lookup_client, mock_slack_service
    ):
        fingerprint = "33333333-3333-4333-8333-333333333333"
        mock_lookup_client.lookup.side_effect = httpx.ConnectError("boom")
        app = _make_app(lookup_client=mock_lookup_client, slack_service=mock_slack_service)

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja by juana molina"},
                    headers={"X-Device-Fingerprint": fingerprint},
                )

        assert response.status_code == 200
        mock_slack_service.post_blocks.assert_awaited_once()
        call = mock_slack_service.post_blocks.call_args
        assert call.kwargs["metadata"] == {
            "event_type": "request_posted",
            "event_payload": {"fingerprint": fingerprint},
        }
        assert fingerprint not in str(call.args[0])

    @pytest.mark.asyncio
    async def test_no_fingerprint_header_omits_metadata_on_degraded_path(
        self, mock_lookup_client, mock_slack_service
    ):
        app = _make_app(lookup_client=mock_lookup_client, slack_service=mock_slack_service)

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            side_effect=ValueError("broken"),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                )

        assert response.status_code == 200
        mock_slack_service.post_blocks.assert_awaited_once()
        assert mock_slack_service.post_blocks.call_args.kwargs["metadata"] is None


# -----------------------------------------------------------------------------
# Slack-down: still surfaces as 502.
# -----------------------------------------------------------------------------


class TestSlackStillFailsLoudly:
    """Slack remains the only hard dependency: failures there are not silenced."""

    @pytest.mark.asyncio
    async def test_slack_post_failure_returns_502_in_lml_degraded_path(
        self, mock_lookup_client, mock_slack_service
    ):
        mock_lookup_client.lookup.side_effect = httpx.ConnectError("lml down")
        mock_slack_service.post_blocks.side_effect = httpx.HTTPError("slack down")
        app = _make_app(lookup_client=mock_lookup_client, slack_service=mock_slack_service)

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                )

        assert response.status_code == 502
        assert "Slack" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_slack_post_failure_returns_502_in_groq_degraded_path(
        self, mock_lookup_client, mock_slack_service
    ):
        mock_slack_service.post_blocks.side_effect = httpx.HTTPError("slack down")
        app = _make_app(lookup_client=mock_lookup_client, slack_service=mock_slack_service)

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            side_effect=ValueError("groq down"),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                )

        assert response.status_code == 502
        assert "Slack" in response.json()["detail"]


# -----------------------------------------------------------------------------
# Slack disabled (None service): degraded path stays a no-op for posting.
# -----------------------------------------------------------------------------


class TestDegradedTelemetry:
    """PostHog events must carry the degraded_mode signal so we can alert on outages."""

    def _make_app_with_posthog(self, *, lookup_client, slack_service, posthog_client):
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        app.dependency_overrides[get_groq_client] = lambda: Mock()
        app.dependency_overrides[get_slack_service] = lambda: slack_service
        app.dependency_overrides[get_posthog_client] = lambda: posthog_client
        app.dependency_overrides[get_lookup_client] = lambda: lookup_client
        return app

    @pytest.mark.asyncio
    async def test_lml_degraded_records_search_unavailable_in_posthog(
        self, mock_lookup_client, mock_slack_service
    ):
        mock_lookup_client.lookup.side_effect = httpx.ConnectError("boom")
        posthog = Mock()
        posthog.capture = Mock()
        app = self._make_app_with_posthog(
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=posthog,
        )

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.post("/api/v1/request", json={"message": "play queen"})

        posthog.capture.assert_called()
        properties = posthog.capture.call_args.kwargs.get("properties") or (
            posthog.capture.call_args.args[2] if len(posthog.capture.call_args.args) >= 3 else {}
        )
        assert properties.get("degraded_mode") == "search_unavailable"
        assert properties.get("degraded_reason") == "ConnectError"

    @pytest.mark.asyncio
    async def test_groq_degraded_records_parsing_unavailable_in_posthog(
        self, mock_lookup_client, mock_slack_service
    ):
        posthog = Mock()
        posthog.capture = Mock()
        app = self._make_app_with_posthog(
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=posthog,
        )

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            side_effect=ValueError("groq down"),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.post("/api/v1/request", json={"message": "play queen"})

        posthog.capture.assert_called()
        properties = posthog.capture.call_args.kwargs.get("properties") or (
            posthog.capture.call_args.args[2] if len(posthog.capture.call_args.args) >= 3 else {}
        )
        assert properties.get("degraded_mode") == "parsing_unavailable"
        assert properties.get("degraded_reason") == "ValueError"


class TestSlackDisabled:
    """When Slack integration is off, degraded paths still return 200 with no post."""

    @pytest.mark.asyncio
    async def test_lml_degraded_with_slack_off(self, mock_lookup_client):
        mock_lookup_client.lookup.side_effect = httpx.ConnectError("boom")
        app = _make_app(lookup_client=mock_lookup_client, slack_service=None)

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                )

        assert response.status_code == 200
        assert response.json()["degraded_mode"] == "search_unavailable"

    @pytest.mark.asyncio
    async def test_groq_degraded_with_slack_off(self, mock_lookup_client):
        app = _make_app(lookup_client=mock_lookup_client, slack_service=None)

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            side_effect=ValueError("broken"),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                )

        assert response.status_code == 200
        assert response.json()["degraded_mode"] == "parsing_unavailable"
