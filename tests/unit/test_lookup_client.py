"""Unit tests for services/lookup_client.py."""

import json

import httpx
import pytest

from services.lookup_client import (
    LookupRequest,
    LookupResponse,
    LookupResult,
    LookupResultItem,
    LookupServiceClient,
)


def _make_client(handler, retry_delay=0, **kwargs) -> LookupServiceClient:
    """Create a LookupServiceClient with a mock transport."""
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    return LookupServiceClient(
        "http://test-service/api/v1", http_client, retry_delay=retry_delay, **kwargs
    )


SAMPLE_RESPONSE = {
    "results": [
        {
            "library_item": {
                "id": 42,
                "title": "Aluminum Tunes",
                "artist": "Stereolab",
                "call_letters": "S",
                "artist_call_number": 1,
                "release_call_number": 2,
                "genre": "Rock",
                "format": "CD",
                "call_number": "Rock CD S 1/2",
                "library_url": "http://www.wxyc.info/wxycdb/libraryRelease?id=42",
            },
            "artwork": {
                "album": "Aluminum Tunes",
                "artist": "Stereolab",
                "release_id": 123,
                "release_url": "https://discogs.com/release/123",
                "artwork_url": "https://img.discogs.com/test.jpg",
                "confidence": 0.95,
            },
        }
    ],
    "search_type": "direct",
    "song_not_found": False,
    "found_on_compilation": False,
    "context_message": None,
    "corrected_artist": None,
    "cache_stats": {
        "memory_hits": 1,
        "pg_hits": 0,
        "pg_misses": 0,
        "api_calls": 0,
        "pg_time_ms": 0.0,
        "api_time_ms": 0.0,
    },
}


class TestLookupServiceClient:
    """Tests for LookupServiceClient."""

    @pytest.mark.asyncio
    async def test_successful_lookup(self):
        """Successful lookup parses response correctly."""

        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v1/lookup"
            assert request.method == "POST"
            body = json.loads(request.content)
            assert body["artist"] == "Stereolab"
            assert body["song"] == "Ping Pong"
            assert body["raw_message"] == "play ping pong by stereolab"
            assert "album" not in body  # exclude_none=True
            return httpx.Response(200, json=SAMPLE_RESPONSE)

        client = _make_client(handler)
        result = await client.lookup(
            LookupRequest(
                artist="Stereolab",
                song="Ping Pong",
                raw_message="play ping pong by stereolab",
            )
        )

        # lookup() returns a LookupResult pairing the parsed response with the
        # raw Server-Timing header (None here — the handler sends no header).
        assert isinstance(result, LookupResult)
        assert result.server_timing is None
        response = result.response
        assert isinstance(response, LookupResponse)
        assert response.results is not None
        assert len(response.results) == 1
        assert response.results[0].library_item.id == 42
        assert response.results[0].library_item.artist == "Stereolab"
        assert response.results[0].artwork is not None
        assert response.results[0].artwork.artwork_url == "https://img.discogs.com/test.jpg"
        assert response.search_type == "direct"
        assert response.song_not_found is False
        assert response.found_on_compilation is False

    @pytest.mark.asyncio
    async def test_lookup_captures_server_timing_header(self):
        """When LML returns a Server-Timing header, it is captured on the result
        so the router can forward and merge the sub-stage breakdown."""

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=SAMPLE_RESPONSE,
                headers={
                    "Server-Timing": "library_search;dur=41.2, discogs;dur=806, total;dur=8560.1"
                },
            )

        client = _make_client(handler)
        result = await client.lookup(
            LookupRequest(artist="Stereolab", raw_message="play stereolab")
        )

        assert isinstance(result, LookupResult)
        assert result.server_timing == "library_search;dur=41.2, discogs;dur=806, total;dur=8560.1"
        assert result.response.search_type == "direct"

    @pytest.mark.asyncio
    async def test_successful_lookup_with_all_fields(self):
        """Lookup with all fields sends them in request body."""

        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["artist"] == "Juana Molina"
            assert body["song"] == "la paradoja"
            assert body["album"] == "DOGA"
            return httpx.Response(200, json=SAMPLE_RESPONSE)

        client = _make_client(handler)
        await client.lookup(
            LookupRequest(
                artist="Juana Molina",
                song="la paradoja",
                album="DOGA",
                raw_message="play la paradoja",
            )
        )

    @pytest.mark.asyncio
    async def test_empty_results(self):
        """Lookup returning no results parses correctly."""
        empty_response = {
            "results": [],
            "search_type": "none",
            "song_not_found": True,
            "found_on_compilation": False,
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=empty_response)

        client = _make_client(handler)
        response = (
            await client.lookup(
                LookupRequest(artist="ZZZNONEXISTENT", raw_message="play ZZZNONEXISTENT")
            )
        ).response

        assert response.results == []
        assert response.search_type == "none"
        assert response.song_not_found is True

    @pytest.mark.asyncio
    async def test_corrected_artist(self):
        """Lookup with corrected artist is parsed correctly."""
        response_data = {
            **SAMPLE_RESPONSE,
            "corrected_artist": "Living Colour",
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=response_data)

        client = _make_client(handler)
        response = (
            await client.lookup(
                LookupRequest(artist="Living Color", raw_message="play living color")
            )
        ).response

        assert response.corrected_artist == "Living Colour"

    @pytest.mark.asyncio
    async def test_http_500_raises_status_error(self):
        """HTTP 500 from service raises httpx.HTTPStatusError."""

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "Internal server error"})

        client = _make_client(handler)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await client.lookup(LookupRequest(artist="Stereolab", raw_message="play stereolab"))
        assert exc_info.value.response.status_code == 500

    @pytest.mark.asyncio
    async def test_http_422_raises_status_error(self):
        """HTTP 422 (validation error) raises httpx.HTTPStatusError."""

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(422, json={"detail": "Validation error"})

        client = _make_client(handler)
        with pytest.raises(httpx.HTTPStatusError):
            await client.lookup(LookupRequest(artist="Stereolab", raw_message="play stereolab"))

    @pytest.mark.asyncio
    async def test_connection_error_propagates(self):
        """Connection errors propagate as httpx.ConnectError."""

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        client = _make_client(handler)
        with pytest.raises(httpx.ConnectError):
            await client.lookup(LookupRequest(artist="Stereolab", raw_message="play stereolab"))

    @pytest.mark.asyncio
    async def test_timeout_propagates(self):
        """Timeout errors propagate as httpx.ReadTimeout."""

        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("Read timed out")

        client = _make_client(handler)
        with pytest.raises(httpx.ReadTimeout):
            await client.lookup(LookupRequest(artist="Stereolab", raw_message="play stereolab"))

    @pytest.mark.asyncio
    async def test_base_url_trailing_slash_stripped(self):
        """Trailing slash on base_url is handled correctly."""

        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v1/lookup"
            return httpx.Response(200, json=SAMPLE_RESPONSE)

        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(transport=transport)
        client = LookupServiceClient("http://test-service/api/v1/", http_client)

        await client.lookup(LookupRequest(artist="Queen", raw_message="play queen"))

    @pytest.mark.asyncio
    async def test_skip_cache_sends_query_param(self):
        """skip_cache=True sends skip_cache query parameter."""

        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["skip_cache"] == "true"
            return httpx.Response(200, json=SAMPLE_RESPONSE)

        client = _make_client(handler)
        await client.lookup(
            LookupRequest(artist="Stereolab", raw_message="play stereolab"),
            skip_cache=True,
        )

    @pytest.mark.asyncio
    async def test_no_skip_cache_omits_query_param(self):
        """skip_cache=False (default) sends no query parameter."""

        async def handler(request: httpx.Request) -> httpx.Response:
            assert "skip_cache" not in request.url.params
            return httpx.Response(200, json=SAMPLE_RESPONSE)

        client = _make_client(handler)
        await client.lookup(LookupRequest(artist="Queen", raw_message="play queen"))

    @pytest.mark.asyncio
    async def test_bearer_header_sent_when_api_key_configured(self):
        """When api_key is configured, every lookup sends Authorization: Bearer <key>."""

        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("authorization") == "Bearer test-token-abc123"
            return httpx.Response(200, json=SAMPLE_RESPONSE)

        client = _make_client(handler, api_key="test-token-abc123")
        await client.lookup(LookupRequest(artist="Stereolab", raw_message="play stereolab"))

    @pytest.mark.asyncio
    async def test_no_authorization_header_when_api_key_unset(self):
        """When api_key is None, no Authorization header is sent (back-compat)."""

        async def handler(request: httpx.Request) -> httpx.Response:
            assert "authorization" not in {k.lower() for k in request.headers.keys()}
            return httpx.Response(200, json=SAMPLE_RESPONSE)

        client = _make_client(handler)  # api_key omitted -> None
        await client.lookup(LookupRequest(artist="Stereolab", raw_message="play stereolab"))

    @pytest.mark.asyncio
    async def test_bearer_header_sent_on_retry(self):
        """The Authorization header is sent on every attempt, not just the first."""
        calls = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.headers.get("authorization"))
            if len(calls) == 1:
                raise httpx.ConnectError("Connection refused")
            return httpx.Response(200, json=SAMPLE_RESPONSE)

        client = _make_client(handler, api_key="retry-token")
        await client.lookup(LookupRequest(artist="Cat Power", raw_message="play cat power"))
        assert calls == ["Bearer retry-token", "Bearer retry-token"]

    @pytest.mark.asyncio
    async def test_caller_budget_header_default_from_timeout(self):
        """With the default 20s per-attempt timeout, X-Caller-Budget-Ms is 19800."""

        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["x-caller-budget-ms"] == "19800"
            return httpx.Response(200, json=SAMPLE_RESPONSE)

        client = _make_client(handler)  # per_attempt_timeout defaults to 20.0
        await client.lookup(LookupRequest(artist="Stereolab", raw_message="play stereolab"))

    @pytest.mark.asyncio
    async def test_caller_budget_header_explicit_wins(self):
        """An explicit caller_budget_ms is sent verbatim, independent of the timeout."""

        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["x-caller-budget-ms"] == "5000"
            return httpx.Response(200, json=SAMPLE_RESPONSE)

        # per_attempt_timeout that would derive 9800 is overridden by the explicit budget.
        client = _make_client(handler, per_attempt_timeout=10.0, caller_budget_ms=5000)
        await client.lookup(LookupRequest(artist="Stereolab", raw_message="play stereolab"))

    @pytest.mark.asyncio
    async def test_caller_budget_header_is_integer_string(self):
        """The header value is a plain integer string, never a float like '19800.0'."""

        async def handler(request: httpx.Request) -> httpx.Response:
            value = request.headers["x-caller-budget-ms"]
            assert "." not in value
            assert int(value) == 19800
            return httpx.Response(200, json=SAMPLE_RESPONSE)

        client = _make_client(handler)
        await client.lookup(LookupRequest(artist="Stereolab", raw_message="play stereolab"))

    @pytest.mark.asyncio
    async def test_caller_budget_header_sent_on_retry(self):
        """The X-Caller-Budget-Ms header rides every attempt, not just the first."""
        calls = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.headers.get("x-caller-budget-ms"))
            if len(calls) == 1:
                raise httpx.ConnectError("Connection refused")
            return httpx.Response(200, json=SAMPLE_RESPONSE)

        client = _make_client(handler)  # per_attempt_timeout defaults to 20.0
        await client.lookup(LookupRequest(artist="Cat Power", raw_message="play cat power"))
        assert calls == ["19800", "19800"]

    @pytest.mark.asyncio
    async def test_caller_budget_header_clamps_to_floor(self):
        """A tiny per-attempt timeout clamps the derived budget to the 1000ms floor."""

        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["x-caller-budget-ms"] == "1000"
            return httpx.Response(200, json=SAMPLE_RESPONSE)

        client = _make_client(handler, per_attempt_timeout=0.5)
        await client.lookup(LookupRequest(artist="Stereolab", raw_message="play stereolab"))


class TestLookupModels:
    """Tests for lookup models."""

    def test_lookup_request_exclude_none(self):
        """LookupRequest excludes None fields when dumped."""
        req = LookupRequest(artist="Stereolab", raw_message="play stereolab")
        dumped = req.model_dump(exclude_none=True)
        assert "artist" in dumped
        assert "raw_message" in dumped
        assert "song" not in dumped
        assert "album" not in dumped

    def test_lookup_request_all_fields(self):
        """LookupRequest includes all fields when set."""
        req = LookupRequest(
            artist="Juana Molina", song="la paradoja", album="DOGA", raw_message="msg"
        )
        dumped = req.model_dump(exclude_none=True)
        assert dumped == {
            "artist": "Juana Molina",
            "song": "la paradoja",
            "album": "DOGA",
            "raw_message": "msg",
            # include_identity is a non-optional bool with a False default, so it
            # survives exclude_none (added to the LookupRequest contract upstream).
            "include_identity": False,
        }

    def test_lookup_response_defaults(self):
        """LookupResponse has correct defaults."""
        resp = LookupResponse()
        assert resp.results == []
        assert resp.search_type == "none"
        assert resp.song_not_found is False
        assert resp.found_on_compilation is False
        assert resp.context_message is None
        assert resp.corrected_artist is None
        assert resp.cache_stats is None

    def test_lookup_result_item_without_artwork(self):
        """LookupResultItem works without artwork."""
        from tests.factories import make_library_item

        item = LookupResultItem(library_item=make_library_item(id=1))
        assert item.artwork is None
        assert item.library_item.id == 1


class TestLookupRetry:
    """Tests for retry logic in LookupServiceClient."""

    @pytest.mark.asyncio
    async def test_retry_on_connect_error_then_success(self):
        """First call raises ConnectError, second succeeds."""
        calls = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ConnectError("Connection refused")
            return httpx.Response(200, json=SAMPLE_RESPONSE)

        client = _make_client(handler)
        result = await client.lookup(
            LookupRequest(artist="Stereolab", raw_message="play stereolab")
        )
        assert len(calls) == 2
        assert isinstance(result, LookupResult)
        assert isinstance(result.response, LookupResponse)

    @pytest.mark.asyncio
    async def test_no_retry_on_timeout(self):
        """ReadTimeout raises immediately; retrying a slow lookup just doubles LML load."""
        calls = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            raise httpx.ReadTimeout("Read timed out")

        client = _make_client(handler)
        with pytest.raises(httpx.ReadTimeout):
            await client.lookup(LookupRequest(artist="Cat Power", raw_message="play cat power"))
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_retry_exhausted_connect_error(self):
        """Both calls raise ConnectError. Exception propagates."""
        calls = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            raise httpx.ConnectError("Connection refused")

        client = _make_client(handler)
        with pytest.raises(httpx.ConnectError):
            await client.lookup(LookupRequest(artist="Stereolab", raw_message="play stereolab"))
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_no_retry_on_http_status_error(self):
        """HTTP 500 raises immediately, no retry."""
        calls = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(500, json={"detail": "Internal server error"})

        client = _make_client(handler)
        with pytest.raises(httpx.HTTPStatusError):
            await client.lookup(LookupRequest(artist="Stereolab", raw_message="play stereolab"))
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_no_retry_on_http_422(self):
        """HTTP 422 raises immediately, no retry."""
        calls = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(422, json={"detail": "Validation error"})

        client = _make_client(handler)
        with pytest.raises(httpx.HTTPStatusError):
            await client.lookup(LookupRequest(artist="Stereolab", raw_message="play stereolab"))
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_successful_request_no_retry(self):
        """Successful first call, no retry attempted."""
        calls = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, json=SAMPLE_RESPONSE)

        client = _make_client(handler)
        result = await client.lookup(
            LookupRequest(artist="Jessica Pratt", raw_message="play jessica pratt")
        )
        assert len(calls) == 1
        assert isinstance(result, LookupResult)
        assert isinstance(result.response, LookupResponse)
