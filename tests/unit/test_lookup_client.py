"""Unit tests for services/lookup_client.py."""

import json

import httpx
import pytest

from services.lookup_client import (
    LookupRequest,
    LookupResponse,
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
    "cache_stats": {"hits": 1, "misses": 0},
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
        response = await client.lookup(
            LookupRequest(
                artist="Stereolab",
                song="Ping Pong",
                raw_message="play ping pong by stereolab",
            )
        )

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
        response = await client.lookup(
            LookupRequest(artist="ZZZNONEXISTENT", raw_message="play ZZZNONEXISTENT")
        )

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
        response = await client.lookup(
            LookupRequest(artist="Living Color", raw_message="play living color")
        )

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
        response = await client.lookup(
            LookupRequest(artist="Stereolab", raw_message="play stereolab")
        )
        assert len(calls) == 2
        assert isinstance(response, LookupResponse)

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
        response = await client.lookup(
            LookupRequest(artist="Jessica Pratt", raw_message="play jessica pratt")
        )
        assert len(calls) == 1
        assert isinstance(response, LookupResponse)
