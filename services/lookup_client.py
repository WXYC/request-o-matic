"""HTTP client for library-metadata-lookup service."""

import asyncio
import logging

import httpx
from pydantic import BaseModel

from models import DiscogsSearchResult, LibraryItem

logger = logging.getLogger(__name__)


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
    """Thin async client for library-metadata-lookup service.

    Args:
        base_url: Base URL of the lookup service (e.g. ``http://host:8080/api/v1``)
        http_client: Shared ``httpx.AsyncClient``
        max_attempts: Total attempts per lookup (1 = no retry, 2 = one retry)
        retry_delay: Seconds to wait between retries
        per_attempt_timeout: Per-attempt timeout in seconds (overrides the client default)
    """

    _RETRYABLE = (httpx.ConnectError, httpx.TimeoutException)

    def __init__(
        self,
        base_url: str,
        http_client: httpx.AsyncClient,
        *,
        max_attempts: int = 2,
        retry_delay: float = 1.0,
        per_attempt_timeout: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.http_client = http_client
        self.max_attempts = max_attempts
        self.retry_delay = retry_delay
        self.per_attempt_timeout = per_attempt_timeout

    async def lookup(self, request: LookupRequest, skip_cache: bool = False) -> LookupResponse:
        """Call the lookup service and return parsed response.

        Retries on transient network errors (``ConnectError``, ``TimeoutException``).
        HTTP status errors are raised immediately without retry.

        Args:
            request: Lookup request with artist/song/album/raw_message
            skip_cache: If True, bypass the lookup service's caches

        Returns:
            LookupResponse with results and metadata

        Raises:
            httpx.HTTPStatusError: On 4xx/5xx responses (no retry)
            httpx.ConnectError: If the service is unreachable after all attempts
            httpx.TimeoutException: If the request times out after all attempts
        """
        params = {"skip_cache": "true"} if skip_cache else {}
        last_exc: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = await self.http_client.post(
                    f"{self.base_url}/lookup",
                    json=request.model_dump(exclude_none=True),
                    params=params,
                    timeout=self.per_attempt_timeout,
                )
                response.raise_for_status()
                return LookupResponse.model_validate(response.json())
            except self._RETRYABLE as exc:
                last_exc = exc
                if attempt < self.max_attempts:
                    logger.warning(
                        "Lookup attempt %d/%d failed (%s: %s), retrying in %.1fs",
                        attempt,
                        self.max_attempts,
                        type(exc).__name__,
                        exc,
                        self.retry_delay,
                    )
                    await asyncio.sleep(self.retry_delay)

        raise last_exc  # type: ignore[misc]
