"""HTTP client for library-metadata-lookup service."""

import asyncio
import logging

import httpx

from generated.api_models import LookupRequest, LookupResponse, LookupResultItem

__all__ = ["LookupRequest", "LookupResponse", "LookupResultItem", "LookupServiceClient"]

logger = logging.getLogger(__name__)


class LookupServiceClient:
    """Thin async client for library-metadata-lookup service.

    Args:
        base_url: Base URL of the lookup service (e.g. ``http://host:8080/api/v1``)
        http_client: Shared ``httpx.AsyncClient``
        api_key: Optional ``LML_API_KEY``. When set, every request includes
            ``Authorization: Bearer <api_key>``. Required when LML has
            ``LML_REQUIRE_AUTH=true``.
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
        api_key: str | None = None,
        max_attempts: int = 2,
        retry_delay: float = 1.0,
        per_attempt_timeout: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.http_client = http_client
        self.api_key = api_key
        self.max_attempts = max_attempts
        self.retry_delay = retry_delay
        self.per_attempt_timeout = per_attempt_timeout

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

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
                    headers=self._auth_headers(),
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
