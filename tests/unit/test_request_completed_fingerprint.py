"""Unit tests for the ``fingerprint`` property on ``request_completed`` telemetry.

WXYC/request-o-matic#216. `request_completed` fires from two places in
`handle_request`: the parsing-degraded early return and the main/search-degraded
return at the end. Both must carry `fingerprint` when the caller sent
`X-Device-Fingerprint`, and must omit it (never the string "None") when absent.
`distinct_id` must stay the constant `request-o-matic-service` either way.
"""

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastapi import FastAPI
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

FINGERPRINT = "11111111-2222-3333-4444-555555555555"


def _make_app(*, lookup_client, slack_service, posthog_client):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_groq_client] = lambda: Mock()
    app.dependency_overrides[get_slack_service] = lambda: slack_service
    app.dependency_overrides[get_posthog_client] = lambda: posthog_client
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


@pytest.fixture
def posthog():
    client = Mock()
    client.capture = Mock()
    return client


def _request_completed_call(posthog):
    """`posthog.capture` also fires per-step `request_parse` events; find the
    `request_completed` call specifically."""
    for call in posthog.capture.call_args_list:
        event = call.kwargs.get("event") or (call.args[1] if len(call.args) >= 2 else None)
        if event == "request_completed":
            return call
    raise AssertionError("request_completed was never captured")


def _captured_properties(posthog):
    call = _request_completed_call(posthog)
    return call.kwargs.get("properties") or (call.args[2] if len(call.args) >= 3 else {})


def _captured_distinct_id(posthog):
    call = _request_completed_call(posthog)
    return call.kwargs.get("distinct_id") or (call.args[0] if call.args else None)


class TestMainPathFingerprint:
    """The main/search-degraded return at the end of handle_request."""

    @pytest.mark.asyncio
    async def test_fingerprint_present_is_included(
        self, mock_lookup_client, mock_slack_service, posthog
    ):
        mock_lookup_client.lookup.side_effect = httpx.ConnectError("lml down")
        app = _make_app(
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
                await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja by juana molina"},
                    headers={"X-Device-Fingerprint": FINGERPRINT},
                )

        properties = _captured_properties(posthog)
        assert properties.get("fingerprint") == FINGERPRINT
        assert _captured_distinct_id(posthog) == "request-o-matic-service"

    @pytest.mark.asyncio
    async def test_fingerprint_absent_is_omitted(
        self, mock_lookup_client, mock_slack_service, posthog
    ):
        mock_lookup_client.lookup.side_effect = httpx.ConnectError("lml down")
        app = _make_app(
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
                await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja by juana molina"},
                )

        properties = _captured_properties(posthog)
        assert "fingerprint" not in properties
        assert _captured_distinct_id(posthog) == "request-o-matic-service"


class TestParsingDegradedPathFingerprint:
    """The parsing-degraded early return (Groq down)."""

    @pytest.mark.asyncio
    async def test_fingerprint_present_is_included(
        self, mock_lookup_client, mock_slack_service, posthog
    ):
        app = _make_app(
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
                await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                    headers={"X-Device-Fingerprint": FINGERPRINT},
                )

        properties = _captured_properties(posthog)
        assert properties.get("fingerprint") == FINGERPRINT
        assert _captured_distinct_id(posthog) == "request-o-matic-service"

    @pytest.mark.asyncio
    async def test_fingerprint_absent_is_omitted(
        self, mock_lookup_client, mock_slack_service, posthog
    ):
        app = _make_app(
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
                await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                )

        properties = _captured_properties(posthog)
        assert "fingerprint" not in properties
        assert _captured_distinct_id(posthog) == "request-o-matic-service"
