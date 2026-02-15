"""HTTP client for library-metadata-lookup service."""

import httpx
from pydantic import BaseModel

from discogs.models import DiscogsSearchResult
from library.models import LibraryItem


class LookupRequest(BaseModel):
    """Request body for the lookup service."""

    artist: str | None = None
    song: str | None = None
    album: str | None = None
    raw_message: str


class LookupResultItem(BaseModel):
    """A single lookup result: library item paired with optional artwork."""

    library_item: LibraryItem
    artwork: DiscogsSearchResult | None = None


class LookupResponse(BaseModel):
    """Response from the lookup service."""

    results: list[LookupResultItem] = []
    search_type: str = "none"
    song_not_found: bool = False
    found_on_compilation: bool = False
    context_message: str | None = None
    corrected_artist: str | None = None
    cache_stats: dict | None = None


class LookupServiceClient:
    """Thin async client for library-metadata-lookup service."""

    def __init__(self, base_url: str, http_client: httpx.AsyncClient):
        self.base_url = base_url.rstrip("/")
        self.http_client = http_client

    async def lookup(self, request: LookupRequest, skip_cache: bool = False) -> LookupResponse:
        """Call the lookup service and return parsed response.

        Args:
            request: Lookup request with artist/song/album/raw_message
            skip_cache: If True, bypass the lookup service's caches

        Returns:
            LookupResponse with results and metadata

        Raises:
            httpx.HTTPStatusError: On 4xx/5xx responses
            httpx.ConnectError: If the service is unreachable
            httpx.TimeoutException: If the request times out
        """
        params = {"skip_cache": "true"} if skip_cache else {}
        response = await self.http_client.post(
            f"{self.base_url}/lookup",
            json=request.model_dump(exclude_none=True),
            params=params,
        )
        response.raise_for_status()
        return LookupResponse.model_validate(response.json())
