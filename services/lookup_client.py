"""HTTP client for library-metadata-lookup service."""

import asyncio
import logging
from typing import NamedTuple

import httpx

from generated.api_models import LookupRequest, LookupResponse, LookupResultItem

__all__ = [
    "LookupRequest",
    "LookupResponse",
    "LookupResult",
    "LookupResultItem",
    "LookupServiceClient",
]

logger = logging.getLogger(__name__)


class LookupResult(NamedTuple):
    """The parsed LML response paired with its raw ``Server-Timing`` header.

    ``server_timing`` is the verbatim header value LML returns (or ``None`` when
    absent), carried alongside the body so the router can forward and merge LML's
    per-stage timings without re-fetching. Returned by value rather than stashed
    on the client instance so ``lookup()`` stays a pure function of its inputs —
    the client wraps a shared ``httpx.AsyncClient`` singleton, so per-call state
    on the instance would be fragile if the wrapper were ever cached too.
    """

    response: LookupResponse
    server_timing: str | None


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
        caller_budget_ms: Value sent as the ``X-Caller-Budget-Ms`` header on every
            ``POST /lookup``. LML uses it to bound its search-pipeline budget instead
            of falling back to its ``LML_SEARCH_BUDGET_MS`` default. When ``None``
            (the default), it is derived from ``per_attempt_timeout``:
            ``max(int(per_attempt_timeout * 1000) - 200, 1000)`` — the 200 ms trims
            transport overhead so LML returns a graceful in-budget result before ROM's
            httpx client times out, and the 1000 ms floor guards tiny/zero timeouts.
            An explicit value is used verbatim.
    """

    # Only retry transient connection-establishment failures. TimeoutException
    # is deliberately excluded: a lookup that already spent the per-attempt
    # budget is doing real work on LML's side and won't be faster on retry,
    # while the concurrent duplicate doubles LML's Discogs/Apple Music load
    # because LML cannot cancel work when the rom socket disconnects.
    _RETRYABLE = (httpx.ConnectError,)

    def __init__(
        self,
        base_url: str,
        http_client: httpx.AsyncClient,
        *,
        api_key: str | None = None,
        max_attempts: int = 2,
        retry_delay: float = 1.0,
        per_attempt_timeout: float = 20.0,
        caller_budget_ms: int | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.http_client = http_client
        self.api_key = api_key
        self.max_attempts = max_attempts
        self.retry_delay = retry_delay
        self.per_attempt_timeout = per_attempt_timeout
        # An explicit budget wins verbatim; otherwise derive it from the per-attempt
        # timeout, shaving 200ms of transport overhead so LML can return a graceful
        # in-budget result before ROM's httpx client times out, with a 1000ms floor.
        self._effective_caller_budget_ms = (
            caller_budget_ms
            if caller_budget_ms is not None
            else max(int(self.per_attempt_timeout * 1000) - 200, 1000)
        )

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    async def lookup(self, request: LookupRequest, skip_cache: bool = False) -> LookupResult:
        """Call the lookup service and return the parsed response + timing header.

        Retries on ``ConnectError`` only. ``TimeoutException`` and HTTP status
        errors are raised immediately without retry.

        Args:
            request: Lookup request with artist/song/album/raw_message
            skip_cache: If True, bypass the lookup service's caches

        Returns:
            LookupResult pairing the parsed ``LookupResponse`` with LML's raw
            ``Server-Timing`` header value (``None`` when the header is absent).

        Raises:
            httpx.HTTPStatusError: On 4xx/5xx responses (no retry)
            httpx.ConnectError: If the service is unreachable after all attempts
            httpx.TimeoutException: If the request times out (no retry)
        """
        params = {"skip_cache": "true"} if skip_cache else {}
        last_exc: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = await self.http_client.post(
                    f"{self.base_url}/lookup",
                    json=request.model_dump(exclude_none=True),
                    params=params,
                    headers={
                        **self._auth_headers(),
                        "X-Caller-Budget-Ms": str(self._effective_caller_budget_ms),
                    },
                    timeout=self.per_attempt_timeout,
                )
                response.raise_for_status()
                return LookupResult(
                    response=LookupResponse.model_validate(response.json()),
                    server_timing=response.headers.get("Server-Timing"),
                )
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
