"""Unit tests for request-line ban enforcement on POST /request.

WXYC/request-o-matic#150 / WXYC/Backend-Service#1261. The ban check runs after
the empty-message guard and BEFORE parse, so a banned listener never consumes
Groq TPM or LML cache budget.

Behavior matrix:
- valid + unbanned  → proceed (LML + Slack as usual)
- valid + banned    → 403, no Slack, no Groq, no LML, request_blocked telemetry
- BS 401 / 404      → proceed-as-unauth (don't 401 the caller)
- no headers        → proceed AND don't call BS (saves the v3.1 round-trip)
- fingerprint-only + banned → 403
- BS unreachable    → fail-open: proceed, log warning, emit
                      degraded_mode=ban_check_unavailable telemetry
- feature flag off  → never call BS, never 403
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import httpx
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
from generated.api_models import CacheStats, SearchType
from routers.request import router
from services.ban_check_client import (
    BanCheckClient,
    BanCheckResult,
    BanCheckUnavailableError,
)
from services.lookup_client import LookupResponse, LookupResult, LookupServiceClient
from tests.conftest import make_parsed_request

SAMPLE_PARSED = make_parsed_request(
    song="la paradoja",
    artist="Juana Molina",
    raw_message="play la paradoja by juana molina",
)

EMPTY_LOOKUP = LookupResponse(
    results=[],
    search_type=SearchType.none,
    song_not_found=False,
    found_on_compilation=False,
    context_message=None,
    corrected_artist=None,
    cache_stats=CacheStats(
        memory_hits=0,
        pg_hits=0,
        pg_misses=0,
        api_calls=0,
        pg_time_ms=0.0,
        api_time_ms=0.0,
    ),
)


def _make_app(
    *,
    ban_check_client: BanCheckClient | None,
    lookup_client: LookupServiceClient | None = None,
    slack_service: AsyncMock | None = None,
    posthog_client: Mock | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_groq_client] = lambda: Mock()
    app.dependency_overrides[get_slack_service] = lambda: slack_service
    app.dependency_overrides[get_posthog_client] = lambda: posthog_client
    app.dependency_overrides[get_lookup_client] = lambda: lookup_client
    app.dependency_overrides[get_ban_check_client] = lambda: ban_check_client
    return app


@pytest.fixture
def mock_ban_check_client():
    return AsyncMock(spec=BanCheckClient)


@pytest.fixture
def mock_lookup_client():
    client = AsyncMock(spec=LookupServiceClient)
    client.lookup.return_value = LookupResult(response=EMPTY_LOOKUP, server_timing=None)
    return client


@pytest.fixture
def mock_slack_service():
    svc = AsyncMock()
    svc.post_blocks = AsyncMock()
    svc.webhook_url = "https://hooks.slack.com/test"
    return svc


@pytest.fixture
def mock_posthog():
    posthog = Mock()
    posthog.capture = Mock()
    return posthog


# ---------------------------------------------------------------------------
# Banned-caller path
# ---------------------------------------------------------------------------


class TestBannedCallerBlocked:
    """When BS says banned, return 403 and skip the whole pipeline."""

    @pytest.mark.asyncio
    async def test_user_ban_returns_403_no_slack_no_groq(
        self,
        mock_ban_check_client,
        mock_lookup_client,
        mock_slack_service,
        mock_posthog,
    ):
        mock_ban_check_client.check.return_value = BanCheckResult(
            banned=True,
            user_id="user-banned",
            fingerprint="11111111-1111-4111-8111-111111111111",
            ban_reason="Repeated abuse",
            ban_source="user",
        )
        app = _make_app(
            ban_check_client=mock_ban_check_client,
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=mock_posthog,
        )

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            return_value=SAMPLE_PARSED,
        ) as mock_parse:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                    headers={
                        "Authorization": "Bearer eyJ...",
                        "X-Device-Fingerprint": "11111111-1111-4111-8111-111111111111",
                    },
                )

        assert response.status_code == 403
        mock_ban_check_client.check.assert_awaited_once()
        # Verify the headers were forwarded
        call_kwargs = mock_ban_check_client.check.call_args.kwargs
        assert call_kwargs["authorization"] == "Bearer eyJ..."
        assert call_kwargs["fingerprint"] == "11111111-1111-4111-8111-111111111111"
        # Shadow-ban: no Groq, no LML, no Slack
        mock_parse.assert_not_awaited()
        mock_lookup_client.lookup.assert_not_awaited()
        mock_slack_service.post_blocks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fingerprint_only_ban_returns_403(
        self,
        mock_ban_check_client,
        mock_lookup_client,
        mock_slack_service,
        mock_posthog,
    ):
        mock_ban_check_client.check.return_value = BanCheckResult(
            banned=True,
            user_id=None,
            fingerprint="22222222-2222-4222-8222-222222222222",
            ban_reason="Spam",
            ban_source="fingerprint",
        )
        app = _make_app(
            ban_check_client=mock_ban_check_client,
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=mock_posthog,
        )

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            return_value=SAMPLE_PARSED,
        ) as mock_parse:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                    headers={
                        "X-Device-Fingerprint": "22222222-2222-4222-8222-222222222222",
                    },
                )

        assert response.status_code == 403
        # Header was forwarded as fingerprint, authorization is None
        call_kwargs = mock_ban_check_client.check.call_args.kwargs
        assert call_kwargs["authorization"] is None
        assert call_kwargs["fingerprint"] == "22222222-2222-4222-8222-222222222222"
        mock_parse.assert_not_awaited()
        mock_lookup_client.lookup.assert_not_awaited()
        mock_slack_service.post_blocks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ban_emits_request_blocked_posthog_event(
        self,
        mock_ban_check_client,
        mock_lookup_client,
        mock_slack_service,
        mock_posthog,
    ):
        mock_ban_check_client.check.return_value = BanCheckResult(
            banned=True,
            user_id="user-xyz",
            fingerprint="33333333-3333-4333-8333-333333333333",
            ban_reason="Hate speech",
            ban_source="user",
        )
        app = _make_app(
            ban_check_client=mock_ban_check_client,
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=mock_posthog,
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post(
                "/api/v1/request",
                json={"message": "anything"},
                headers={
                    "Authorization": "Bearer eyJ...",
                    "X-Device-Fingerprint": "33333333-3333-4333-8333-333333333333",
                },
            )

        # Find the request_blocked event among capture calls.
        blocked_calls = [
            c
            for c in mock_posthog.capture.call_args_list
            if c.kwargs.get("event") == "request_blocked"
        ]
        assert len(blocked_calls) == 1, "Expected exactly one request_blocked event"
        props = blocked_calls[0].kwargs["properties"]
        assert props["user_id"] == "user-xyz"
        assert props["fingerprint"] == "33333333-3333-4333-8333-333333333333"
        assert props["ban_reason"] == "Hate speech"
        assert props["ban_source"] == "user"


# ---------------------------------------------------------------------------
# Allowed-caller path
# ---------------------------------------------------------------------------


class TestAllowedCallerProceeds:
    """When BS says not banned, the request proceeds normally."""

    @pytest.mark.asyncio
    async def test_unbanned_user_proceeds(
        self,
        mock_ban_check_client,
        mock_lookup_client,
        mock_slack_service,
        mock_posthog,
    ):
        mock_ban_check_client.check.return_value = BanCheckResult(
            banned=False, user_id="user-good", fingerprint=None
        )
        app = _make_app(
            ban_check_client=mock_ban_check_client,
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=mock_posthog,
        )

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            return_value=SAMPLE_PARSED,
        ) as mock_parse:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                    headers={"Authorization": "Bearer eyJ..."},
                )

        assert response.status_code == 200
        mock_ban_check_client.check.assert_awaited_once()
        mock_parse.assert_awaited_once()
        mock_lookup_client.lookup.assert_awaited_once()
        mock_slack_service.post_blocks.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_headers_skips_bs_call(
        self,
        mock_ban_check_client,
        mock_lookup_client,
        mock_slack_service,
        mock_posthog,
    ):
        """v3.1 iOS clients send no Authorization header — must not call BS."""
        app = _make_app(
            ban_check_client=mock_ban_check_client,
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=mock_posthog,
        )

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            return_value=SAMPLE_PARSED,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                )

        assert response.status_code == 200
        mock_ban_check_client.check.assert_not_awaited()
        mock_lookup_client.lookup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_proceed_as_unauth_on_invalid_token(
        self,
        mock_ban_check_client,
        mock_lookup_client,
        mock_slack_service,
        mock_posthog,
    ):
        """BS 401 (invalid JWT) → BanCheckResult(banned=False); proceed.

        The caller MUST NOT see 401 on POST /request.
        """
        mock_ban_check_client.check.return_value = BanCheckResult(banned=False)
        app = _make_app(
            ban_check_client=mock_ban_check_client,
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=mock_posthog,
        )

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            return_value=SAMPLE_PARSED,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                    headers={"Authorization": "Bearer garbage"},
                )

        assert response.status_code == 200
        mock_ban_check_client.check.assert_awaited_once()
        mock_lookup_client.lookup.assert_awaited_once()


# ---------------------------------------------------------------------------
# Fail-open path
# ---------------------------------------------------------------------------


class TestBanCheckUnreachableFailsOpen:
    """When BS is unreachable, proceed with the request (don't 502 the caller)."""

    @pytest.mark.asyncio
    async def test_bs_unreachable_proceeds(
        self,
        mock_ban_check_client,
        mock_lookup_client,
        mock_slack_service,
        mock_posthog,
    ):
        mock_ban_check_client.check.side_effect = BanCheckUnavailableError("BS down")
        app = _make_app(
            ban_check_client=mock_ban_check_client,
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=mock_posthog,
        )

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            return_value=SAMPLE_PARSED,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                    headers={"Authorization": "Bearer eyJ..."},
                )

        # Fail open: caller sees a normal 200, the pipeline runs.
        assert response.status_code == 200
        mock_lookup_client.lookup.assert_awaited_once()
        mock_slack_service.post_blocks.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_bs_unreachable_emits_ban_check_degraded_telemetry(
        self,
        mock_ban_check_client,
        mock_lookup_client,
        mock_slack_service,
        mock_posthog,
    ):
        mock_ban_check_client.check.side_effect = BanCheckUnavailableError("BS down")
        app = _make_app(
            ban_check_client=mock_ban_check_client,
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=mock_posthog,
        )

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            return_value=SAMPLE_PARSED,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                    headers={"Authorization": "Bearer eyJ..."},
                )

        # The summary completed event should carry both degraded_mode and the
        # ban_check_degraded property (since LML succeeded, degraded_mode falls
        # back to ban_check_unavailable).
        completed_calls = [
            c
            for c in mock_posthog.capture.call_args_list
            if c.kwargs.get("event") == "request_completed"
        ]
        assert len(completed_calls) == 1
        props = completed_calls[0].kwargs["properties"]
        assert props.get("ban_check_degraded") is True
        assert props.get("degraded_mode") == "ban_check_unavailable"

    @pytest.mark.asyncio
    async def test_lml_outage_takes_precedence_in_degraded_mode_field(
        self,
        mock_ban_check_client,
        mock_lookup_client,
        mock_slack_service,
        mock_posthog,
    ):
        """If both BS and LML are down, degraded_mode=search_unavailable wins.

        ban_check_degraded is still emitted as its own property so operators
        can see both signals.
        """
        mock_ban_check_client.check.side_effect = BanCheckUnavailableError("BS down")
        mock_lookup_client.lookup.side_effect = httpx.ConnectError("LML down")
        app = _make_app(
            ban_check_client=mock_ban_check_client,
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=mock_posthog,
        )

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            return_value=SAMPLE_PARSED,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                    headers={"Authorization": "Bearer eyJ..."},
                )

        assert response.status_code == 200
        assert response.json()["degraded_mode"] == "search_unavailable"
        completed_calls = [
            c
            for c in mock_posthog.capture.call_args_list
            if c.kwargs.get("event") == "request_completed"
        ]
        props = completed_calls[0].kwargs["properties"]
        assert props.get("degraded_mode") == "search_unavailable"
        assert props.get("ban_check_degraded") is True


# ---------------------------------------------------------------------------
# Feature flag off
# ---------------------------------------------------------------------------


class TestFeatureFlagOff:
    """When the feature flag is off, the dependency returns None and BS is never called."""

    @pytest.mark.asyncio
    async def test_no_ban_check_client_skips_check(
        self, mock_lookup_client, mock_slack_service, mock_posthog
    ):
        app = _make_app(
            ban_check_client=None,
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=mock_posthog,
        )

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            return_value=SAMPLE_PARSED,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                    headers={
                        "Authorization": "Bearer eyJ...",
                        "X-Device-Fingerprint": "44444444-4444-4444-8444-444444444444",
                    },
                )

        assert response.status_code == 200
        mock_lookup_client.lookup.assert_awaited_once()


# ---------------------------------------------------------------------------
# Skip-call optimization
# ---------------------------------------------------------------------------


class TestSkipBSWhenNoSignal:
    """When the caller supplies no *usable* signal, skip BS even if the client is wired."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "fingerprint",
        [
            pytest.param("not-a-uuid", id="malformed"),
            pytest.param("   ", id="whitespace-only"),
            pytest.param("abc?inject=evil", id="query-injection"),
        ],
    )
    async def test_unusable_fingerprint_only_does_not_call_bs(
        self,
        mock_ban_check_client,
        mock_lookup_client,
        mock_slack_service,
        mock_posthog,
        fingerprint,
    ):
        """A malformed fingerprint is no signal at all, so the gate must skip
        BS rather than round-trip.

        Gating on the raw header's truthiness instead let the request reach
        ``check()``, which drops the malformed value and then raises
        ``ValueError`` because nothing survived. Nothing catches that, so a
        listener got a 500 from ``POST /request`` merely for sending a garbage
        fingerprint.
        """
        app = _make_app(
            ban_check_client=mock_ban_check_client,
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=mock_posthog,
        )

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            return_value=SAMPLE_PARSED,
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
        mock_ban_check_client.check.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_headers_does_not_call_bs(
        self,
        mock_ban_check_client,
        mock_lookup_client,
        mock_slack_service,
        mock_posthog,
    ):
        app = _make_app(
            ban_check_client=mock_ban_check_client,
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=mock_posthog,
        )

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            return_value=SAMPLE_PARSED,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                )

        assert response.status_code == 200
        mock_ban_check_client.check.assert_not_awaited()
