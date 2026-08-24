"""Unit tests for caller-identity properties on request telemetry.

WXYC/request-o-matic#278. Before this, ROM recorded nothing about *who* called
it: `request_completed` and `request_non_request` carried the parse result and
(since #216) a `fingerprint` when one was usable, but no `User-Agent`, and no
way to tell an *absent* `X-Device-Fingerprint` from a *present but malformed*
one. Half of production's traffic was consequently unattributable during the
2026-08-24 investigation.

Three properties close that, on both events:

- `user_agent` -- verbatim, client-declared, diagnostic only. Never gate on it.
- `has_fingerprint` -- the header carried a value `POST /admin/bans` will accept.
- `fingerprint_malformed` -- the header was present but not a UUID.

The two booleans are deliberately independent rather than a single tri-state
string: `request_blocked` already splits them this way (#226), and a boolean
survives a PostHog property-type inference that a string enum does not.

`fingerprint` itself is unchanged -- #216 owns it, and #209 settled that the
value is never *displayed*. Nothing here widens its exposure: the malformed
branch records only that a malformed value arrived, never the value.
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
from generated.api_models import SearchType
from routers.request import router
from services.lookup_client import LookupResponse, LookupResult
from services.parser import MessageType
from tests.conftest import REQUEST_MESSAGE, make_parsed_request, make_request_app

FINGERPRINT = "11111111-2222-3333-4444-555555555555"
MALFORMED_FINGERPRINT = "not-a-uuid"
USER_AGENT = "WXYC-iOS/3.2.1 (iPhone; iOS 18.5)"

NON_REQUEST_MESSAGE = "love the show!"


@pytest.fixture
def posthog():
    client = Mock()
    client.capture = Mock()
    return client


@pytest.fixture
def mock_slack_service():
    svc = AsyncMock()
    svc.post_blocks = AsyncMock()
    svc.webhook_url = "https://hooks.slack.com/test"
    return svc


def _make_non_request_app(*, slack_service, posthog_client):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_groq_client] = lambda: Mock()
    app.dependency_overrides[get_slack_service] = lambda: slack_service
    app.dependency_overrides[get_posthog_client] = lambda: posthog_client
    app.dependency_overrides[get_lookup_client] = lambda: None
    app.dependency_overrides[get_ban_check_client] = lambda: None
    return app


async def _post(app, *, message, fingerprint=None, user_agent=USER_AGENT):
    headers = {}
    if fingerprint is not None:
        headers["X-Device-Fingerprint"] = fingerprint
    if user_agent is not None:
        headers["User-Agent"] = user_agent
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/api/v1/request", json={"message": message}, headers=headers)


def _captured_properties(posthog, event_name):
    for call in posthog.capture.call_args_list:
        event = call.kwargs.get("event") or (call.args[1] if len(call.args) >= 2 else None)
        if event == event_name:
            return call.kwargs.get("properties") or (call.args[2] if len(call.args) >= 3 else {})
    raise AssertionError(f"{event_name} was never captured")


# (header value, expected has_fingerprint, expected fingerprint_malformed)
FINGERPRINT_DISPOSITIONS = [
    pytest.param(FINGERPRINT, True, False, id="usable"),
    pytest.param(MALFORMED_FINGERPRINT, False, True, id="malformed"),
    pytest.param(None, False, False, id="absent"),
]


class TestRequestCompletedCallerVisibility:
    """The main return at the end of `handle_request` -- a parsed song request
    with LML answering normally."""

    def _app(self, mock_lookup_client, mock_slack_service, posthog):
        mock_lookup_client.lookup.return_value = LookupResult(
            response=LookupResponse(results=[], search_type=SearchType.none),
            server_timing=None,
        )
        return make_request_app(
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=posthog,
        )

    @pytest.mark.asyncio
    async def test_user_agent_is_recorded_verbatim(
        self, mock_lookup_client, mock_slack_service, posthog, parsed_request
    ):
        app = self._app(mock_lookup_client, mock_slack_service, posthog)

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=parsed_request
        ):
            await _post(app, message=REQUEST_MESSAGE)

        assert _captured_properties(posthog, "request_completed")["user_agent"] == USER_AGENT

    @pytest.mark.asyncio
    @pytest.mark.parametrize("header,expected_has,expected_malformed", FINGERPRINT_DISPOSITIONS)
    async def test_fingerprint_disposition(
        self,
        mock_lookup_client,
        mock_slack_service,
        posthog,
        parsed_request,
        header,
        expected_has,
        expected_malformed,
    ):
        app = self._app(mock_lookup_client, mock_slack_service, posthog)

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=parsed_request
        ):
            await _post(app, message=REQUEST_MESSAGE, fingerprint=header)

        properties = _captured_properties(posthog, "request_completed")
        assert properties["has_fingerprint"] is expected_has
        assert properties["fingerprint_malformed"] is expected_malformed

    @pytest.mark.asyncio
    async def test_malformed_value_is_never_recorded(
        self, mock_lookup_client, mock_slack_service, posthog, parsed_request
    ):
        """The disposition flags say a malformed value arrived; they must not
        smuggle the value itself into a long-retention sink (#209)."""
        app = self._app(mock_lookup_client, mock_slack_service, posthog)

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=parsed_request
        ):
            await _post(app, message=REQUEST_MESSAGE, fingerprint=MALFORMED_FINGERPRINT)

        properties = _captured_properties(posthog, "request_completed")
        assert MALFORMED_FINGERPRINT not in str(properties.values())
        assert "fingerprint" not in properties


class TestNonRequestCallerVisibility:
    """The `if not parsed.is_request:` branch, which emits `request_non_request`
    (#228) rather than `request_completed`."""

    @pytest.mark.asyncio
    async def test_user_agent_is_recorded_verbatim(self, mock_slack_service, posthog):
        parsed = make_parsed_request(
            is_request=False,
            message_type=MessageType.FEEDBACK,
            raw_message=NON_REQUEST_MESSAGE,
        )
        app = _make_non_request_app(slack_service=mock_slack_service, posthog_client=posthog)

        with patch("routers.request.parse_request", new_callable=AsyncMock, return_value=parsed):
            await _post(app, message=NON_REQUEST_MESSAGE)

        assert _captured_properties(posthog, "request_non_request")["user_agent"] == USER_AGENT

    @pytest.mark.asyncio
    @pytest.mark.parametrize("header,expected_has,expected_malformed", FINGERPRINT_DISPOSITIONS)
    async def test_fingerprint_disposition(
        self, mock_slack_service, posthog, header, expected_has, expected_malformed
    ):
        parsed = make_parsed_request(
            is_request=False,
            message_type=MessageType.FEEDBACK,
            raw_message=NON_REQUEST_MESSAGE,
        )
        app = _make_non_request_app(slack_service=mock_slack_service, posthog_client=posthog)

        with patch("routers.request.parse_request", new_callable=AsyncMock, return_value=parsed):
            await _post(app, message=NON_REQUEST_MESSAGE, fingerprint=header)

        properties = _captured_properties(posthog, "request_non_request")
        assert properties["has_fingerprint"] is expected_has
        assert properties["fingerprint_malformed"] is expected_malformed


class TestNoNewEvents:
    """#278's binding constraint: properties on existing events, never a new
    event name. The org is on the free tier at its six-project limit and came
    off a quota exhaustion on 2026-08-04."""

    @pytest.mark.asyncio
    async def test_only_known_events_are_captured(
        self, mock_lookup_client, mock_slack_service, posthog, parsed_request
    ):
        mock_lookup_client.lookup.return_value = LookupResult(
            response=LookupResponse(results=[], search_type=SearchType.none),
            server_timing=None,
        )
        app = make_request_app(
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=posthog,
        )

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=parsed_request
        ):
            await _post(app, message=REQUEST_MESSAGE, fingerprint=FINGERPRINT)

        captured = {
            call.kwargs.get("event") or (call.args[1] if len(call.args) >= 2 else None)
            for call in posthog.capture.call_args_list
        }
        assert captured <= {
            "request_completed",
            "request_parse",
            "request_lookup_service",
            "request_slack_post",
        }
