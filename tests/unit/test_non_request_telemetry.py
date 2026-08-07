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
    get_ban_check_client,
    get_groq_client,
    get_lookup_client,
    get_posthog_client,
    get_slack_service,
)
from routers.request import router
from services.ban_check_client import BanCheckUnavailableError
from services.parser import MessageType
from tests.conftest import make_parsed_request

MESSAGE = "love the show!"

FINGERPRINT = "11111111-2222-3333-4444-555555555555"
MALFORMED_FINGERPRINT = "not-a-uuid"


def _make_app(*, slack_service, posthog_client, ban_check_client=None):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_groq_client] = lambda: Mock()
    app.dependency_overrides[get_slack_service] = lambda: slack_service
    app.dependency_overrides[get_posthog_client] = lambda: posthog_client
    app.dependency_overrides[get_lookup_client] = lambda: None
    app.dependency_overrides[get_ban_check_client] = lambda: ban_check_client
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


async def _post(app, *, fingerprint: str | None = None, skip_slack: bool = False):
    # skip_slack defaults to False so the Slack post immediately preceding the
    # code under test is actually exercised -- production never sends True, and
    # skipping it would leave the emit's ordering relative to that call untested.
    headers = {} if fingerprint is None else {"X-Device-Fingerprint": fingerprint}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            "/api/v1/request",
            json={"message": MESSAGE, "skip_slack": skip_slack},
            headers=headers,
        )


def _non_request_call(posthog):
    matches = [
        call
        for call in posthog.capture.call_args_list
        if (call.kwargs.get("event") or (call.args[1] if len(call.args) >= 2 else None))
        == "request_non_request"
    ]
    if not matches:
        raise AssertionError("request_non_request was never captured")
    # Exactly one: a double emit would double-count every chatter device in the
    # runbook's `ORDER BY requests DESC` leaderboard, which is the one ordering
    # the ban workflow depends on.
    assert len(matches) == 1, f"expected exactly one request_non_request, got {len(matches)}"
    return matches[0]


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

    @pytest.mark.asyncio
    async def test_message_still_reaches_slack(self, mock_slack_service, posthog):
        """The emit must not displace the Slack post -- the whole premise is
        that this message IS visible to DJs while its sender was not."""
        parsed = make_parsed_request(
            is_request=False,
            message_type=MessageType.FEEDBACK,
            raw_message=MESSAGE,
        )
        app = _make_app(slack_service=mock_slack_service, posthog_client=posthog)

        with patch("routers.request.parse_request", new_callable=AsyncMock, return_value=parsed):
            response = await _post(app, fingerprint=FINGERPRINT)

        assert response.status_code == 200
        mock_slack_service.post_blocks.assert_awaited_once()
        _captured_properties(posthog)

    @pytest.mark.asyncio
    async def test_ban_check_outage_sets_both_degraded_signals(self, mock_slack_service, posthog):
        """A BS outage must set `degraded_mode` as well as the boolean, matching
        the main emit. A dashboard keyed on `degraded_mode` would otherwise
        undercount the outage by the entire non-request share of traffic.
        """
        ban_check_client = AsyncMock()
        ban_check_client.check.side_effect = BanCheckUnavailableError("BS down")
        parsed = make_parsed_request(
            is_request=False,
            message_type=MessageType.FEEDBACK,
            raw_message=MESSAGE,
        )
        app = _make_app(
            slack_service=mock_slack_service,
            posthog_client=posthog,
            ban_check_client=ban_check_client,
        )

        with patch("routers.request.parse_request", new_callable=AsyncMock, return_value=parsed):
            response = await _post(app, fingerprint=FINGERPRINT)

        # Fail-open: the listener is served regardless.
        assert response.status_code == 200
        properties = _captured_properties(posthog)
        assert properties.get("ban_check_degraded") is True
        assert properties.get("degraded_mode") == "ban_check_unavailable"

    @pytest.mark.asyncio
    async def test_no_posthog_client_does_not_break_the_response(self, mock_slack_service):
        """Telemetry is best-effort: with no PostHog client configured the
        listener still gets a 200 and the message still reaches Slack."""
        parsed = make_parsed_request(
            is_request=False,
            message_type=MessageType.FEEDBACK,
            raw_message=MESSAGE,
        )
        app = _make_app(slack_service=mock_slack_service, posthog_client=None)

        with patch("routers.request.parse_request", new_callable=AsyncMock, return_value=parsed):
            response = await _post(app, fingerprint=FINGERPRINT)

        assert response.status_code == 200
        mock_slack_service.post_blocks.assert_awaited_once()
