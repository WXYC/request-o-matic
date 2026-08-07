"""Unit tests for the `request_non_request` telemetry event.

WXYC/request-o-matic#228. Before this, the `if not parsed.is_request:` early
return in `handle_request` posted to Slack and returned without emitting any
telemetry at all -- not even `request_completed` -- so a device sending only
non-request messages (the likeliest shape of request-line abuse, since it
never touches `POST /admin/bans`-eligible ban logic) left no bannable
fingerprint behind. See the design discussion on #228: this is deliberately a
*new* event name, not `request_completed` with `is_request=False`, so the
existing `request_completed` series keeps its meaning.

`fingerprint` must be the normalized value (never the raw header) for the
same reasons as the sibling emit sites in
tests/unit/test_request_completed_fingerprint.py, and `distinct_id` must stay
the constant `request-o-matic-service`.
"""

from unittest.mock import AsyncMock, Mock, patch

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
from services.parser import MessageType
from tests.conftest import make_parsed_request

MESSAGE = "love the show!"

FINGERPRINT = "11111111-2222-3333-4444-555555555555"
MALFORMED_FINGERPRINT = "not-a-uuid"


def _make_app(*, slack_service, posthog_client):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_groq_client] = lambda: Mock()
    app.dependency_overrides[get_slack_service] = lambda: slack_service
    app.dependency_overrides[get_posthog_client] = lambda: posthog_client
    app.dependency_overrides[get_lookup_client] = lambda: None
    return app


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


async def _post(app, *, fingerprint: str | None = None):
    headers = {} if fingerprint is None else {"X-Device-Fingerprint": fingerprint}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            "/api/v1/request",
            json={"message": MESSAGE, "skip_slack": True},
            headers=headers,
        )


def _non_request_call(posthog):
    for call in posthog.capture.call_args_list:
        event = call.kwargs.get("event") or (call.args[1] if len(call.args) >= 2 else None)
        if event == "request_non_request":
            return call
    raise AssertionError("request_non_request was never captured")


def _captured_properties(posthog):
    call = _non_request_call(posthog)
    return call.kwargs.get("properties") or (call.args[2] if len(call.args) >= 3 else {})


def _captured_distinct_id(posthog):
    call = _non_request_call(posthog)
    return call.kwargs.get("distinct_id") or (call.args[0] if call.args else None)


class TestNonRequestTelemetry:
    @pytest.mark.asyncio
    async def test_valid_fingerprint_is_included(self, mock_slack_service, posthog):
        parsed = make_parsed_request(
            is_request=False,
            message_type=MessageType.FEEDBACK,
            raw_message=MESSAGE,
        )
        app = _make_app(slack_service=mock_slack_service, posthog_client=posthog)

        with patch("routers.request.parse_request", new_callable=AsyncMock, return_value=parsed):
            response = await _post(app, fingerprint=FINGERPRINT)

        assert response.status_code == 200
        properties = _captured_properties(posthog)
        assert properties.get("fingerprint") == FINGERPRINT
        assert properties.get("message_type") == "feedback"
        assert _captured_distinct_id(posthog) == "request-o-matic-service"

    @pytest.mark.asyncio
    async def test_malformed_fingerprint_is_not_recorded(self, mock_slack_service, posthog):
        parsed = make_parsed_request(
            is_request=False,
            message_type=MessageType.FEEDBACK,
            raw_message=MESSAGE,
        )
        app = _make_app(slack_service=mock_slack_service, posthog_client=posthog)

        with patch("routers.request.parse_request", new_callable=AsyncMock, return_value=parsed):
            response = await _post(app, fingerprint=MALFORMED_FINGERPRINT)

        assert response.status_code == 200
        properties = _captured_properties(posthog)
        assert "fingerprint" not in properties

    @pytest.mark.asyncio
    async def test_no_fingerprint_is_omitted(self, mock_slack_service, posthog):
        parsed = make_parsed_request(
            is_request=False,
            message_type=MessageType.DJ_MESSAGE,
            raw_message=MESSAGE,
        )
        app = _make_app(slack_service=mock_slack_service, posthog_client=posthog)

        with patch("routers.request.parse_request", new_callable=AsyncMock, return_value=parsed):
            response = await _post(app)

        assert response.status_code == 200
        properties = _captured_properties(posthog)
        assert "fingerprint" not in properties
        assert properties.get("message_type") == "dj_message"
        assert _captured_distinct_id(posthog) == "request-o-matic-service"

    @pytest.mark.asyncio
    async def test_request_completed_is_not_also_emitted(self, mock_slack_service, posthog):
        """The non-request branch must not resurrect `request_completed` --
        that series has meant "a song request went through the pipeline"
        since it went silent on `is_request=False` in Feb 2026 (#228)."""
        parsed = make_parsed_request(
            is_request=False,
            message_type=MessageType.FEEDBACK,
            raw_message=MESSAGE,
        )
        app = _make_app(slack_service=mock_slack_service, posthog_client=posthog)

        with patch("routers.request.parse_request", new_callable=AsyncMock, return_value=parsed):
            await _post(app, fingerprint=FINGERPRINT)

        events = {
            call.kwargs.get("event") or (call.args[1] if len(call.args) >= 2 else None)
            for call in posthog.capture.call_args_list
        }
        assert "request_completed" not in events
