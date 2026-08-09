"""Capture-budget test for WXYC/request-o-matic#251.

Pins the per-request PostHog event count for the clean `/request` flow so a
future change to `handle_request` (an added `track_step`, an extra capture)
can't silently regress ROM's own telemetry volume the way LML's per-step
`lookup_*` events did in the 2026-08-04 org-wide PostHog quota incident (see
WXYC/library-metadata-lookup#1170). ROM's own capture volume is tiny
(~249 `request_*` events/month) and is not itself the incident's cause, but
this test is the structural guard so it stays that way.

Uses `wxyc_fastapi.testing.capture_budget` (>=1.4.0) exactly as
`test_request_completed_fingerprint.py` builds its app, but with a
`CountingPosthog` in place of a bare `Mock()` so the budget is enforced by the
shared helper rather than hand-rolled call-count assertions.
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from wxyc_fastapi.testing import as_posthog, capture_budget

from core.dependencies import (
    get_groq_client,
    get_lookup_client,
    get_posthog_client,
    get_slack_service,
)
from generated.api_models import SearchType
from routers.request import router
from services.lookup_client import LookupResponse, LookupResult, LookupServiceClient
from tests.conftest import make_parsed_request

MESSAGE = "play la paradoja by juana molina"

# The clean `/request` flow tracks three steps (routers/request.py
# `handle_request`): "parse" (always), "lookup_service" (LOOKUP_SERVICE_URL
# configured, LML answers), and "slack_post" (always, once past the
# not-a-request early return). With `emit_step_events=True` (the pinned
# default -- see the "emit_step_events" note in the PR body), `send_to_posthog`
# emits one `request_<step>` event per tracked step plus the `request_completed`
# summary:
#   request_parse + request_lookup_service + request_slack_post + request_completed
# = 4 events. Empirically confirmed via CountingPosthog.events below.
CLEAN_PATH_EVENT_BUDGET = 4


def _make_app(*, lookup_client, slack_service, posthog_client):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_groq_client] = lambda: Mock()
    app.dependency_overrides[get_slack_service] = lambda: slack_service
    app.dependency_overrides[get_posthog_client] = lambda: posthog_client
    app.dependency_overrides[get_lookup_client] = lambda: lookup_client
    return app


@pytest.fixture
def parsed_request():
    """A fresh ParsedRequest per test -- see test_request_completed_fingerprint.py
    for why this can't be a shared module-level constant (the router mutates
    `parsed.artist` in place on the clean path)."""
    return make_parsed_request(
        song="la paradoja",
        artist="juana molina",
        raw_message=MESSAGE,
    )


@pytest.fixture
def mock_lookup_client():
    return AsyncMock(spec=LookupServiceClient)


@pytest.fixture
def mock_slack_service():
    svc = AsyncMock()
    svc.post_blocks = AsyncMock()
    svc.webhook_url = "https://hooks.slack.com/test"
    return svc


async def _post(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/api/v1/request", json={"message": MESSAGE})


class TestCleanPathCaptureBudget:
    """The main return at the end of handle_request, with LML answering
    normally -- no ban, no degraded mode of any kind."""

    @pytest.mark.asyncio
    async def test_clean_request_stays_within_capture_budget(
        self, mock_lookup_client, mock_slack_service, parsed_request
    ):
        mock_lookup_client.lookup.return_value = LookupResult(
            response=LookupResponse(
                results=[],
                search_type=SearchType.none,
                corrected_artist="Juana Molina",
            ),
            server_timing=None,
        )

        with capture_budget(CLEAN_PATH_EVENT_BUDGET) as posthog:
            app = _make_app(
                lookup_client=mock_lookup_client,
                slack_service=mock_slack_service,
                posthog_client=as_posthog(posthog),
            )
            with patch(
                "routers.request.parse_request",
                new_callable=AsyncMock,
                return_value=parsed_request,
            ):
                response = await _post(app)

        assert response.status_code == 200
        # Pin the composition, not just the count: a future change that
        # swaps one event for another of the same name-count would pass a
        # bare `len(...) <= N` check while still changing what's billed.
        assert posthog.events == [
            "request_parse",
            "request_lookup_service",
            "request_slack_post",
            "request_completed",
        ]
        assert posthog.count("request_completed") == 1
