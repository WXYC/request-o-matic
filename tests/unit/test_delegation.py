"""Unit tests for the lookup delegation branch in handle_request."""

import json
import re
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
from services.lookup_client import (
    LookupResponse,
    LookupResult,
    LookupResultItem,
    LookupServiceClient,
)
from tests.conftest import make_parsed_request
from tests.factories import make_library_item, make_release_metadata

# -- Fixtures -----------------------------------------------------------------


def _lr(response: LookupResponse, server_timing: str | None = None) -> LookupResult:
    """Wrap a LookupResponse as the LookupResult that ``lookup()`` now returns.

    These cases assert on response fields, not timing, so they stage a response
    and let the router observe ``server_timing`` (``None`` = no forwarded header)
    exactly as the real client would deliver it.
    """
    return LookupResult(response=response, server_timing=server_timing)


@pytest.fixture
def mock_lookup_client():
    """Create a mock LookupServiceClient."""
    return AsyncMock(spec=LookupServiceClient)


@pytest.fixture
def sample_lookup_response():
    """A typical LookupResponse with one result and artwork."""
    return LookupResponse(
        results=[
            LookupResultItem(
                library_item=make_library_item(
                    id=42,
                    title="Aluminum Tunes",
                    artist="Stereolab",
                    release_call_number=2,
                ),
                artwork=make_release_metadata(
                    release_id=123,
                    album="Aluminum Tunes",
                    artist="Stereolab",
                    artwork_url="https://img.discogs.com/test.jpg",
                    confidence=0.95,
                ),
            )
        ],
        search_type=SearchType.direct,
        song_not_found=False,
        found_on_compilation=False,
        context_message=None,
        corrected_artist=None,
    )


@pytest.fixture
def mock_slack():
    """A mock SlackService whose ``post_blocks`` calls can be inspected."""
    slack = AsyncMock()
    slack.webhook_url = "https://hooks.slack.com/test"
    return slack


@pytest.fixture
def app(mock_lookup_client, mock_slack):
    """Create a test app with the request router and delegation enabled."""
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    mock_groq = Mock()

    app.dependency_overrides[get_groq_client] = lambda: mock_groq
    app.dependency_overrides[get_slack_service] = lambda: mock_slack
    app.dependency_overrides[get_posthog_client] = lambda: None
    app.dependency_overrides[get_lookup_client] = lambda: mock_lookup_client

    return app


SAMPLE_PARSED = make_parsed_request(
    song="la paradoja",
    artist="Juana Molina",
    raw_message="play la paradoja by juana molina",
)


# -- Tests --------------------------------------------------------------------


class TestDelegationBranch:
    """Tests for the delegation branch in handle_request."""

    @pytest.mark.asyncio
    async def test_successful_delegation(self, app, mock_lookup_client, sample_lookup_response):
        """Delegation returns results from lookup service, skipping inline pipeline."""
        mock_lookup_client.lookup.return_value = _lr(sample_lookup_response)

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={
                        "message": "play crazy little thing called love by queen",
                        "skip_slack": True,
                    },
                )

        assert response.status_code == 200
        data = response.json()
        assert data["search_type"] == "direct"
        assert len(data["library_results"]) == 1
        assert data["library_results"][0]["id"] == 42
        assert data["library_results"][0]["artist"] == "Stereolab"
        assert data["artwork"]["artwork_url"] == "https://img.discogs.com/test.jpg"
        assert data["song_not_found"] is False
        assert data["found_on_compilation"] is False

    @pytest.mark.asyncio
    async def test_delegation_maps_response_fields(self, app, mock_lookup_client):
        """All LookupResponse metadata fields map to UnifiedResponse correctly."""
        mock_lookup_client.lookup.return_value = _lr(
            LookupResponse(
                results=[],
                search_type=SearchType.compilation,
                song_not_found=True,
                found_on_compilation=True,
                context_message='Found "Abele Dance" by Manu Dibango on:',
                corrected_artist=None,
            )
        )

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play queen", "skip_slack": True},
                )

        data = response.json()
        assert data["search_type"] == "compilation"
        assert data["song_not_found"] is True
        assert data["found_on_compilation"] is True
        assert data["context_message"] == 'Found "Abele Dance" by Manu Dibango on:'

    @pytest.mark.asyncio
    async def test_corrected_artist_applied(self, app, mock_lookup_client, sample_lookup_response):
        """corrected_artist from lookup replaces parsed.artist in response."""
        sample_lookup_response.corrected_artist = "Living Colour"

        parsed = make_parsed_request(artist="Living Color", raw_message="play living color")
        mock_lookup_client.lookup.return_value = _lr(sample_lookup_response)

        with patch("routers.request.parse_request", new_callable=AsyncMock, return_value=parsed):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play living color", "skip_slack": True},
                )

        data = response.json()
        assert data["parsed"]["artist"] == "Living Colour"

    @pytest.mark.asyncio
    async def test_groq_rate_limit_falls_back_to_parsing_unavailable(self, app, mock_lookup_client):
        """Groq rate limit no longer fails the request — it falls back to parsing-unavailable."""
        from groq import RateLimitError

        resp = httpx.Response(429, request=httpx.Request("POST", "https://api.groq.com/test"))
        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            side_effect=RateLimitError(message="Rate limit exceeded", response=resp, body=None),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play queen", "skip_slack": True},
                )

        assert response.status_code == 200
        assert response.json()["degraded_mode"] == "parsing_unavailable"

    @pytest.mark.asyncio
    async def test_http_error_falls_back_to_search_unavailable(self, app, mock_lookup_client):
        """HTTP error from lookup service no longer 502s — it degrades to search-unavailable."""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.request = Mock()
        mock_lookup_client.lookup.side_effect = httpx.HTTPStatusError(
            "Internal Server Error", request=mock_response.request, response=mock_response
        )

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play queen", "skip_slack": True},
                )

        assert response.status_code == 200
        assert response.json()["degraded_mode"] == "search_unavailable"

    @pytest.mark.asyncio
    async def test_connect_error_falls_back_to_search_unavailable(self, app, mock_lookup_client):
        """Connection error to lookup service no longer 502s — it degrades."""
        mock_lookup_client.lookup.side_effect = httpx.ConnectError("Connection refused")

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play queen", "skip_slack": True},
                )

        assert response.status_code == 200
        assert response.json()["degraded_mode"] == "search_unavailable"

    @pytest.mark.asyncio
    async def test_timeout_falls_back_to_search_unavailable(self, app, mock_lookup_client):
        """Timeout from lookup service no longer 502s — it degrades."""
        mock_lookup_client.lookup.side_effect = httpx.TimeoutException("Read timed out")

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play queen", "skip_slack": True},
                )

        assert response.status_code == 200
        assert response.json()["degraded_mode"] == "search_unavailable"

    @pytest.mark.asyncio
    async def test_skip_cache_forwarded(self, app, mock_lookup_client, sample_lookup_response):
        """skip_cache=True is forwarded to the lookup service."""
        mock_lookup_client.lookup.return_value = _lr(sample_lookup_response)

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.post(
                    "/api/v1/request",
                    json={
                        "message": "play queen",
                        "skip_slack": True,
                        "skip_cache": True,
                    },
                )

        call_args = mock_lookup_client.lookup.call_args
        assert call_args.kwargs.get("skip_cache") is True or (
            len(call_args.args) > 1 and call_args.args[1] is True
        )

    @pytest.mark.asyncio
    async def test_empty_results_from_delegation(self, app, mock_lookup_client):
        """Empty results from lookup service are handled correctly."""
        mock_lookup_client.lookup.return_value = _lr(
            LookupResponse(
                results=[],
                search_type=SearchType.none,
                song_not_found=True,
                found_on_compilation=False,
            )
        )

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play ZZZNONEXISTENT", "skip_slack": True},
                )

        data = response.json()
        assert data["library_results"] == []
        assert data["artwork"] is None
        assert data["search_type"] == "none"
        assert data["song_not_found"] is True

    @pytest.mark.asyncio
    async def test_multiple_results_with_artwork(self, app, mock_lookup_client):
        """Multiple results with artwork are extracted correctly."""
        mock_lookup_client.lookup.return_value = _lr(
            LookupResponse(
                results=[
                    LookupResultItem(
                        library_item=make_library_item(
                            id=1,
                            title="Aluminum Tunes",
                            artist="Stereolab",
                        ),
                        artwork=make_release_metadata(
                            release_id=100,
                            album="Aluminum Tunes",
                            artist="Stereolab",
                            artwork_url="https://img.discogs.com/aluminum.jpg",
                            confidence=0.9,
                        ),
                    ),
                    LookupResultItem(
                        library_item=make_library_item(
                            id=2,
                            title="Dots and Loops",
                            artist="Stereolab",
                            release_call_number=2,
                        ),
                        artwork=None,
                    ),
                ],
                search_type=SearchType.fallback,
            )
        )

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play queen", "skip_slack": True},
                )

        data = response.json()
        assert len(data["library_results"]) == 2
        assert data["library_results"][0]["id"] == 1
        assert data["library_results"][1]["id"] == 2
        # First artwork (non-null) is used as the top-level artwork
        assert data["artwork"]["artwork_url"] == "https://img.discogs.com/aluminum.jpg"
        # Per-item artwork is exposed parallel to library_results so callers can
        # render each release's Discogs URL alongside its library entry.
        assert len(data["result_artworks"]) == 2
        assert data["result_artworks"][0]["release_url"] == "https://www.discogs.com/release/100"
        assert data["result_artworks"][1] is None

    @pytest.mark.asyncio
    async def test_lookup_request_built_from_parsed(
        self, app, mock_lookup_client, sample_lookup_response
    ):
        """LookupRequest is built from parsed fields and raw message."""
        mock_lookup_client.lookup.return_value = _lr(sample_lookup_response)

        parsed = make_parsed_request(
            song="la paradoja",
            album="DOGA",
            artist="Juana Molina",
            raw_message="play la paradoja by juana molina",
        )

        with patch("routers.request.parse_request", new_callable=AsyncMock, return_value=parsed):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.post(
                    "/api/v1/request",
                    json={
                        "message": "play la paradoja by juana molina",
                        "skip_slack": True,
                    },
                )

        lookup_req = mock_lookup_client.lookup.call_args[0][0]
        assert lookup_req.artist == "Juana Molina"
        assert lookup_req.song == "la paradoja"
        assert lookup_req.album == "DOGA"
        assert lookup_req.raw_message == "play la paradoja by juana molina"

    @pytest.mark.asyncio
    async def test_rowless_nonlibrary_results_excluded(self, app, mock_lookup_client, mock_slack):
        """Row-less, non-library results (LML#631: ``id=0``, empty ``library_url``,
        ``call_number="(external)"``) are albums not in the WXYC catalog. A DJ can't
        pull them off the shelf, so they must not reach the request channel — neither
        the response's ``library_results`` nor the Slack post."""
        mock_lookup_client.lookup.return_value = _lr(
            LookupResponse(
                results=[
                    LookupResultItem(
                        library_item=make_library_item(
                            id=42, title="Aluminum Tunes", artist="Stereolab"
                        ),
                        artwork=None,
                    ),
                    LookupResultItem(
                        library_item=make_library_item(
                            id=0,
                            title="Unshelved Bootleg",
                            artist="Not In Library",
                            call_number="(external)",
                            library_url="",
                        ),
                        artwork=make_release_metadata(release_id=999),
                    ),
                ],
                search_type=SearchType.direct,
            )
        )

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post("/api/v1/request", json={"message": "play stereolab"})

        data = response.json()
        # Only the shelved release survives in the response.
        assert [r["id"] for r in data["library_results"]] == [42]

        # The Slack post mentions the library item but never the non-library one.
        mock_slack.post_blocks.assert_awaited_once()
        posted = json.dumps(mock_slack.post_blocks.call_args.args[0])
        assert "Aluminum Tunes" in posted
        assert "Not In Library" not in posted
        assert "Unshelved Bootleg" not in posted

    @pytest.mark.asyncio
    async def test_all_nonlibrary_results_post_no_results_found(
        self, app, mock_lookup_client, mock_slack
    ):
        """When every match is a row-less non-library result, filtering empties the
        list and the request channel falls through to the existing 'No results found'
        message rather than posting a non-library item."""
        mock_lookup_client.lookup.return_value = _lr(
            LookupResponse(
                results=[
                    LookupResultItem(
                        library_item=make_library_item(
                            id=0,
                            title="Unshelved Bootleg",
                            artist="Not In Library",
                            call_number="(external)",
                            library_url="",
                        ),
                        artwork=make_release_metadata(release_id=999),
                    ),
                ],
                search_type=SearchType.direct,
            )
        )

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request", json={"message": "play obscure bootleg"}
                )

        data = response.json()
        assert data["library_results"] == []

        mock_slack.post_blocks.assert_awaited_once()
        posted = json.dumps(mock_slack.post_blocks.call_args.args[0])
        assert "No results found" in posted
        assert "Not In Library" not in posted


class TestFingerprintMetadata:
    """The X-Device-Fingerprint header reaches Slack as chat.postMessage
    metadata, never as visible block text (request-o-matic#209)."""

    @pytest.mark.asyncio
    async def test_fingerprint_header_attaches_metadata(
        self, app, mock_lookup_client, mock_slack, sample_lookup_response
    ):
        fingerprint = "11111111-1111-4111-8111-111111111111"
        mock_lookup_client.lookup.return_value = _lr(sample_lookup_response)

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play stereolab"},
                    headers={"X-Device-Fingerprint": fingerprint},
                )

        assert response.status_code == 200
        mock_slack.post_blocks.assert_awaited_once()
        call = mock_slack.post_blocks.call_args
        assert call.kwargs["metadata"] == {
            "event_type": "request_posted",
            "event_payload": {"fingerprint": fingerprint},
        }
        # Never rendered into the visible blocks -- metadata is the only carrier.
        assert fingerprint not in json.dumps(call.args[0])

    @pytest.mark.asyncio
    async def test_no_fingerprint_header_omits_metadata(
        self, app, mock_lookup_client, mock_slack, sample_lookup_response
    ):
        mock_lookup_client.lookup.return_value = _lr(sample_lookup_response)

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post("/api/v1/request", json={"message": "play stereolab"})

        assert response.status_code == 200
        mock_slack.post_blocks.assert_awaited_once()
        assert mock_slack.post_blocks.call_args.kwargs["metadata"] is None


class TestDelegatedCacheStats:
    """Tests for cache stats propagation in delegated mode.

    When request-o-matic delegates to library-metadata-lookup, all Discogs
    cache/API interactions happen remotely. The lookup service returns its
    cache_stats in the response. These tests verify that the delegated stats
    are surfaced in the UnifiedResponse rather than the local all-zero counters.
    """

    @pytest.mark.asyncio
    async def test_lookup_cache_stats_returned_in_response(
        self, app, mock_lookup_client, sample_lookup_response
    ):
        """Cache stats from lookup service should appear in the response."""
        remote_stats = {
            "memory_hits": 2,
            "pg_hits": 3,
            "pg_misses": 1,
            "api_calls": 4,
            "pg_time_ms": 12.5,
            "api_time_ms": 350.0,
        }
        sample_lookup_response.cache_stats = remote_stats
        mock_lookup_client.lookup.return_value = _lr(sample_lookup_response)

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play queen", "skip_slack": True},
                )

        assert response.status_code == 200
        data = response.json()
        cache_stats = data["cache_stats"]
        assert cache_stats["memory_hits"] == 2
        assert cache_stats["pg_hits"] == 3
        assert cache_stats["pg_misses"] == 1
        assert cache_stats["api_calls"] == 4
        assert cache_stats["pg_time_ms"] == 12.5
        assert cache_stats["api_time_ms"] == 350.0

    @pytest.mark.asyncio
    async def test_lookup_cache_stats_not_all_zeros(
        self, app, mock_lookup_client, sample_lookup_response
    ):
        """Delegated response should NOT report all-zero stats when the lookup
        service reports real activity."""
        remote_stats = {
            "memory_hits": 0,
            "pg_hits": 1,
            "pg_misses": 2,
            "api_calls": 3,
            "pg_time_ms": 8.0,
            "api_time_ms": 200.0,
        }
        sample_lookup_response.cache_stats = remote_stats
        mock_lookup_client.lookup.return_value = _lr(sample_lookup_response)

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play queen", "skip_slack": True},
                )

        data = response.json()
        cache_stats = data["cache_stats"]
        # At least one of these must be non-zero
        assert (cache_stats["pg_hits"] + cache_stats["pg_misses"] + cache_stats["api_calls"]) > 0, (
            "Delegated cache stats should not be all zeros when lookup service reports activity"
        )

    @pytest.mark.asyncio
    async def test_fallback_to_local_stats_when_lookup_returns_none(
        self, app, mock_lookup_client, sample_lookup_response
    ):
        """When lookup service returns no cache_stats, fall back to local counters."""
        sample_lookup_response.cache_stats = None
        mock_lookup_client.lookup.return_value = _lr(sample_lookup_response)

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play queen", "skip_slack": True},
                )

        data = response.json()
        # Should still have cache_stats (from local init_cache_stats), just all zeros
        assert data["cache_stats"] is not None
        assert data["cache_stats"]["memory_hits"] == 0


def _server_timing_names(header: str) -> list[str]:
    """Ordered metric names from a Server-Timing header value."""
    return [entry.split(";")[0].strip() for entry in header.split(",") if entry.strip()]


class TestServerTimingForwarding:
    """Server-Timing emission + LML sub-stage merge on /request (Backend-Service#881).

    /request combines rom's own per-stage telemetry (parse, lookup_service,
    slack_post) with the sub-stages LML forwards in its own Server-Timing header.
    LML's self-measured total is renamed to ``lml_total`` (not dropped) so a
    reader can compute ``lookup_service - lml_total`` = ROM<->LML transport +
    LML framework overhead; the merged header still carries exactly one
    rom-owned ``total``.
    """

    @pytest.mark.asyncio
    async def test_header_present_by_default(self, app, mock_lookup_client, sample_lookup_response):
        """With the flag at its default (on), /request attaches a Server-Timing
        header carrying rom's own stages and exactly one total, last."""
        mock_lookup_client.lookup.return_value = _lr(sample_lookup_response)

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja by juana molina", "skip_slack": True},
                )

        header = response.headers.get("server-timing")
        assert header is not None
        names = _server_timing_names(header)
        assert "parse" in names
        assert "lookup_service" in names
        assert names.count("total") == 1
        assert names[-1] == "total"

    @pytest.mark.asyncio
    async def test_forwarded_lml_substages_merged(
        self, app, mock_lookup_client, sample_lookup_response
    ):
        """LML's forwarded sub-stages are merged in and its own total is renamed
        to ``lml_total`` (not dropped), leaving a strict-parser-safe header with
        exactly one rom-owned ``total``."""
        mock_lookup_client.lookup.return_value = _lr(
            sample_lookup_response,
            server_timing=(
                "library_search;dur=41.2, metadata_enrichment;dur=8500.7, "
                "discogs;dur=806, total;dur=8560.1"
            ),
        )

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "milkman aphex twin", "skip_slack": True},
                )

        header = response.headers["server-timing"]
        entries = [e.strip() for e in header.split(",") if e.strip()]
        names = _server_timing_names(header)
        # LML sub-stages surface, verbatim durations preserved
        assert "library_search" in names
        assert "metadata_enrichment" in names
        assert "discogs" in names
        assert "metadata_enrichment;dur=8500.7" in entries
        # rom's own stages surface
        assert "parse" in names
        assert "lookup_service" in names
        # LML's self-measured total is forwarded, renamed to lml_total, with
        # its original value preserved
        assert "lml_total" in names
        assert "lml_total;dur=8560.1" in entries
        # exactly one total (rom's own), last
        assert names.count("total") == 1
        assert names[-1] == "total"
        # strict-parser-safe wire format (name;dur=<plain-decimal>, ", "-joined)
        grammar = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*;dur=\d+(?:\.\d+)?$")
        for entry in entries:
            assert grammar.match(entry), f"non-conforming Server-Timing entry: {entry!r}"

    @pytest.mark.asyncio
    async def test_new_lml_legs_pass_through(self, app, mock_lookup_client, sample_lookup_response):
        """Forward-looking legs LML is about to start emitting (queue_wait,
        lml_wall, event_loop_lag) pass through the merge untouched, same as any
        other forwarded sub-stage."""
        mock_lookup_client.lookup.return_value = _lr(
            sample_lookup_response,
            server_timing=(
                "queue_wait;dur=12.5, library_search;dur=41.2, event_loop_lag;dur=3.1, "
                "lml_wall;dur=8600.9, total;dur=8560.1"
            ),
        )

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "milkman aphex twin", "skip_slack": True},
                )

        header = response.headers["server-timing"]
        entries = [e.strip() for e in header.split(",") if e.strip()]
        names = _server_timing_names(header)
        assert "queue_wait;dur=12.5" in entries
        assert "event_loop_lag;dur=3.1" in entries
        assert "lml_wall;dur=8600.9" in entries
        assert names.count("total") == 1
        assert names[-1] == "total"

    @pytest.mark.asyncio
    async def test_header_absent_when_flag_disabled(
        self, app, mock_lookup_client, sample_lookup_response
    ):
        """ENABLE_SERVER_TIMING=false suppresses the header entirely."""
        from config.settings import Settings, get_settings

        mock_lookup_client.lookup.return_value = _lr(sample_lookup_response)
        app.dependency_overrides[get_settings] = lambda: Settings(
            groq_api_key="test", enable_server_timing=False
        )

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play stereolab", "skip_slack": True},
                )

        assert response.status_code == 200
        assert "server-timing" not in response.headers

    @pytest.mark.asyncio
    async def test_search_unavailable_still_emits_rom_only_header(self, app, mock_lookup_client):
        """When LML is down, the sentinel keeps the request on the degraded path
        (200, not a 500 from an unbound timing var) and the header carries rom's
        own stages with no forwarded LML sub-stages."""
        mock_lookup_client.lookup.side_effect = httpx.ConnectError("LML down")

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play stereolab", "skip_slack": True},
                )

        assert response.status_code == 200
        assert response.json()["degraded_mode"] == "search_unavailable"
        header = response.headers.get("server-timing")
        assert header is not None
        names = _server_timing_names(header)
        assert "parse" in names
        assert "library_search" not in names
        assert names.count("total") == 1
