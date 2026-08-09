"""Capture-budget tests for WXYC/request-o-matic#251.

Pins the per-request PostHog event count for every branch of the `/request`
flow so a future change to `handle_request` (an added `track_step`, an extra
capture) can't silently regress ROM's own telemetry volume the way LML's
per-step `lookup_*` events did in the 2026-08-04 org-wide PostHog quota
incident (see WXYC/library-metadata-lookup#1170). ROM's own capture volume is
tiny (~249 `request_*` events/month) and is not itself the incident's cause,
but these tests are the structural guard so it stays that way.

Uses `wxyc_fastapi.testing.capture_budget` (>=1.4.0) with the shared
`make_request_app` from `tests/conftest.py`, with a `CountingPosthog` in place
of a bare `Mock()` so the budget is enforced by the shared helper rather than
hand-rolled call-count assertions. Every test also asserts the exact ordered
event list, which is load-bearing: a bare count could not distinguish "budget
respected" from "override never took effect", and would pass a same-count
event swap that still changes what's billed.
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from wxyc_fastapi.testing import as_posthog, capture_budget

from config.settings import Settings, get_settings
from core.dependencies import get_ban_check_client
from generated.api_models import SearchType
from services.lookup_client import LookupResponse, LookupResult
from tests.conftest import REQUEST_MESSAGE as MESSAGE
from tests.conftest import make_parsed_request, make_request_app

# The clean `/request` flow tracks three steps (routers/request.py
# `handle_request`): "parse" (always), "lookup_service" (LOOKUP_SERVICE_URL
# configured, LML answers), and "slack_post" (always, once past the
# not-a-request early return). With `emit_step_events=True` (the pinned
# default -- see the "emit_step_events" note in the PR body), `send_to_posthog`
# emits one `request_<step>` event per tracked step plus the `request_completed`
# summary:
#   request_parse + request_lookup_service + request_slack_post + request_completed
# = 4 events. Empirically confirmed via CountingPosthog.events below.
#
# This is also the service-wide per-request ceiling: every non-clean branch
# (pinned in TestBranchPathCaptureBudgets below) emits strictly fewer events.
CLEAN_PATH_EVENT_BUDGET = 4


async def _post(app, *, headers: dict[str, str] | None = None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/api/v1/request", json={"message": MESSAGE}, headers=headers)


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
            app = make_request_app(
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


class TestBranchPathCaptureBudgets:
    """Every non-clean branch of handle_request, each pinned to its exact
    ordered event list under the clean-path ceiling.

    The two early-return branches (UA-gate blocked, not-a-request) matter
    most: their capture sites (`request_blocked`, `request_non_request`)
    never execute on the clean path, so without these pins a capture added
    only on one of those branches would land unbudgeted.
    """

    @pytest.mark.asyncio
    async def test_ua_gate_blocked_request_emits_only_request_blocked(
        self, mock_lookup_client, mock_slack_service
    ):
        """Known strict client + no fingerprint -> 403 before parse; the
        `request_blocked` capture is the branch's only event
        (WXYC/request-o-matic#155)."""
        with capture_budget(CLEAN_PATH_EVENT_BUDGET) as posthog:
            app = make_request_app(
                lookup_client=mock_lookup_client,
                slack_service=mock_slack_service,
                posthog_client=as_posthog(posthog),
            )
            app.dependency_overrides[get_settings] = lambda: Settings(
                groq_api_key="test_groq_key",
                strict_fingerprint_for_known_clients=True,
            )
            app.dependency_overrides[get_ban_check_client] = lambda: None
            response = await _post(app, headers={"User-Agent": "WXYC-iOS/3.2.0"})

        assert response.status_code == 403
        assert posthog.events == ["request_blocked"]

    @pytest.mark.asyncio
    async def test_non_request_message_emits_only_request_non_request(
        self, mock_lookup_client, mock_slack_service
    ):
        """`is_request=False` early return: one `request_non_request` event,
        never `request_completed` (WXYC/request-o-matic#228) -- the
        `slack_post` step sits after this return, so no step events either."""
        non_request = make_parsed_request(raw_message=MESSAGE, is_request=False)

        with capture_budget(CLEAN_PATH_EVENT_BUDGET) as posthog:
            app = make_request_app(
                lookup_client=mock_lookup_client,
                slack_service=mock_slack_service,
                posthog_client=as_posthog(posthog),
            )
            with patch(
                "routers.request.parse_request",
                new_callable=AsyncMock,
                return_value=non_request,
            ):
                response = await _post(app)

        assert response.status_code == 200
        assert posthog.events == ["request_non_request"]

    @pytest.mark.asyncio
    async def test_parsing_degraded_emits_parse_step_and_summary(
        self, mock_lookup_client, mock_slack_service
    ):
        """Groq failure: `track_step("parse")` records the failed step even on
        exception, then the degraded early return sends telemetry -- so the
        branch emits the parse step event plus the summary and nothing else."""
        with capture_budget(CLEAN_PATH_EVENT_BUDGET) as posthog:
            app = make_request_app(
                lookup_client=mock_lookup_client,
                slack_service=mock_slack_service,
                posthog_client=as_posthog(posthog),
            )
            with patch(
                "routers.request.parse_request",
                new_callable=AsyncMock,
                side_effect=RuntimeError("groq down"),
            ):
                response = await _post(app)

        assert response.status_code == 200
        assert posthog.events == ["request_parse", "request_completed"]

    @pytest.mark.asyncio
    async def test_lookup_unconfigured_emits_three_events(self, mock_slack_service, parsed_request):
        """LML unconfigured (`lookup_client is None`): the `lookup_service`
        step is never entered, so the search-degraded flow emits one fewer
        step event than the clean path."""
        with capture_budget(CLEAN_PATH_EVENT_BUDGET) as posthog:
            app = make_request_app(
                lookup_client=None,
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
        assert posthog.events == [
            "request_parse",
            "request_slack_post",
            "request_completed",
        ]
