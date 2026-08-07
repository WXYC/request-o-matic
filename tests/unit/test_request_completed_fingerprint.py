"""Unit tests for the ``fingerprint`` property on ``request_completed`` telemetry.

WXYC/request-o-matic#216. `request_completed` fires from two places in
`handle_request`: the parsing-degraded early return and the main return at the
end (shared by the clean path and the search-degraded path). Both must carry
`fingerprint` when the caller sent a usable `X-Device-Fingerprint`, and must
omit it (never the string "None") when absent.

"Usable" means "a UUID `POST /admin/bans` will accept" -- the router records
`normalize_fingerprint(header)`, not the raw header, so a malformed value is
recorded exactly like an absent one. A garbage value would be unbannable
through the very flow docs/admin-bans.md documents, and a caller rotating
garbage would fragment into count-1 rows that never surface in that runbook's
leaderboard.

`distinct_id` must stay the constant `request-o-matic-service` throughout:
promoting the fingerprint to `distinct_id` would mint a PostHog person per
device and retroactively re-attribute every historical event.
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
from generated.api_models import SearchType
from routers.request import router
from services.lookup_client import LookupResponse, LookupResult, LookupServiceClient
from tests.conftest import make_parsed_request

MESSAGE = "play la paradoja by juana molina"

FINGERPRINT = "11111111-2222-3333-4444-555555555555"
MALFORMED_FINGERPRINT = "not-a-uuid"


@pytest.fixture
def parsed_request():
    """A fresh ParsedRequest per test.

    Deliberately not a module-level constant: on the clean path the router
    mutates `parsed.artist` in place from `LookupResponse.corrected_artist`, so
    a shared instance would leak that mutation into every later test in the
    file.
    """
    return make_parsed_request(
        song="la paradoja",
        artist="juana molina",
        raw_message=MESSAGE,
    )


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


async def _post(app, *, fingerprint: str | None = None):
    """POST /request, optionally with an ``X-Device-Fingerprint`` header."""
    headers = {} if fingerprint is None else {"X-Device-Fingerprint": fingerprint}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/api/v1/request", json={"message": MESSAGE}, headers=headers)


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


class TestCleanPathFingerprint:
    """The main return at the end of handle_request, with LML answering
    normally -- no degraded mode of any kind.

    `corrected_artist` differs from the fixture's `artist` so the router's
    in-place `parsed.artist = ...` mutation actually fires and is observable.
    That is precisely what the per-test `parsed_request` fixture guards against
    leaking into the other classes.
    """

    @pytest.mark.asyncio
    async def test_fingerprint_present_is_included(
        self, mock_lookup_client, mock_slack_service, posthog, parsed_request
    ):
        mock_lookup_client.lookup.return_value = LookupResult(
            response=LookupResponse(
                results=[],
                search_type=SearchType.none,
                corrected_artist="Juana Molina",
            ),
            server_timing=None,
        )
        app = _make_app(
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=posthog,
        )

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=parsed_request
        ):
            await _post(app, fingerprint=FINGERPRINT)

        properties = _captured_properties(posthog)
        assert properties.get("fingerprint") == FINGERPRINT
        assert _captured_distinct_id(posthog) == "request-o-matic-service"
        # Pin that this really is the clean path and not a degraded one
        # wearing its name -- the defect the previous revision of this file had.
        assert "degraded_mode" not in properties
        # The router mutated the shared-if-not-for-the-fixture ParsedRequest.
        assert parsed_request.artist == "Juana Molina"

    @pytest.mark.asyncio
    async def test_fingerprint_absent_is_omitted(
        self, mock_lookup_client, mock_slack_service, posthog, parsed_request
    ):
        mock_lookup_client.lookup.return_value = LookupResult(
            response=LookupResponse(results=[], search_type=SearchType.none),
            server_timing=None,
        )
        app = _make_app(
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=posthog,
        )

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=parsed_request
        ):
            await _post(app)

        properties = _captured_properties(posthog)
        assert "fingerprint" not in properties
        assert _captured_distinct_id(posthog) == "request-o-matic-service"
        assert "degraded_mode" not in properties

    @pytest.mark.asyncio
    async def test_malformed_fingerprint_is_not_recorded(
        self, mock_lookup_client, mock_slack_service, posthog, parsed_request
    ):
        """A non-UUID is unbannable via POST /admin/bans (422) and, if
        rotated, would bury real devices in the runbook's leaderboard. It is
        recorded exactly like an absent header: not at all."""
        mock_lookup_client.lookup.return_value = LookupResult(
            response=LookupResponse(results=[], search_type=SearchType.none),
            server_timing=None,
        )
        app = _make_app(
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=posthog,
        )

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=parsed_request
        ):
            await _post(app, fingerprint=MALFORMED_FINGERPRINT)

        properties = _captured_properties(posthog)
        assert "fingerprint" not in properties
        assert _captured_distinct_id(posthog) == "request-o-matic-service"
        assert "degraded_mode" not in properties


class TestSearchDegradedPathFingerprint:
    """The same emission line as the clean path above, reached with LML down
    (`search_unavailable`). Pinned separately because the degraded branch skips
    the whole lookup-response block on the way there."""

    @pytest.mark.asyncio
    async def test_fingerprint_present_is_included(
        self, mock_lookup_client, mock_slack_service, posthog, parsed_request
    ):
        mock_lookup_client.lookup.side_effect = httpx.ConnectError("lml down")
        app = _make_app(
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=posthog,
        )

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=parsed_request
        ):
            await _post(app, fingerprint=FINGERPRINT)

        properties = _captured_properties(posthog)
        assert properties.get("fingerprint") == FINGERPRINT
        assert _captured_distinct_id(posthog) == "request-o-matic-service"

    @pytest.mark.asyncio
    async def test_fingerprint_absent_is_omitted(
        self, mock_lookup_client, mock_slack_service, posthog, parsed_request
    ):
        mock_lookup_client.lookup.side_effect = httpx.ConnectError("lml down")
        app = _make_app(
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=posthog,
        )

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=parsed_request
        ):
            await _post(app)

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
            await _post(app, fingerprint=FINGERPRINT)

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
            await _post(app)

        properties = _captured_properties(posthog)
        assert "fingerprint" not in properties
        assert _captured_distinct_id(posthog) == "request-o-matic-service"

    @pytest.mark.asyncio
    async def test_malformed_fingerprint_is_not_recorded(
        self, mock_lookup_client, mock_slack_service, posthog
    ):
        """Both emit sites run the same normalizer -- neither may leak a raw
        header value into telemetry."""
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
            await _post(app, fingerprint=MALFORMED_FINGERPRINT)

        properties = _captured_properties(posthog)
        assert "fingerprint" not in properties
        assert _captured_distinct_id(posthog) == "request-o-matic-service"
