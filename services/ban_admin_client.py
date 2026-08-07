"""HTTP client for Backend-Service's internal banned-fingerprints CRUD (BS#1261).

This is the *only* place in rom that talks to BS's `/internal/banned-fingerprints`
endpoints. Both the HTTP admin router (#151) and the Slack-native ban
router (#152) go through `services/ban_service.py`, which in turn uses this
client. Keeping all BS-shaped concerns here means a contract change on the BS
side (added field, renamed query param, etc.) touches one file.

Response shapes mirror BS exactly — see
`apps/backend/routes/internal-bans.route.ts` for the source of truth:

* POST `/` returns 200 with the upserted row (`fingerprint`, `banned_at`,
  `ban_reason`, `ban_expires_at`, `banned_by_user_id`). Idempotent: a re-ban
  with the same fingerprint succeeds and returns the updated state.
* DELETE `/{fingerprint}` returns 204 with no body. Idempotent: deleting a
  non-existent fingerprint also returns 204.
* GET `/?limit=&cursor=` returns 200 with `{items, nextCursor}` (keyset
  pagination on `(banned_at DESC, fingerprint DESC)`).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


class BanAdminClientError(Exception):
    """Raised when BS rejects an admin call with a 4xx/5xx response, OR when
    the rom->BS hop fails at the transport layer (DNS, TLS, connect-refused,
    socket timeout).

    Transport-layer failures are reported with ``status_code=0`` so the router
    can render them as 502 (upstream unreachable) rather than letting the raw
    httpx exception escape as 500.

    Carries the upstream status code and (best-effort decoded) body so the
    router can surface a faithful status code to the operator instead of
    masking everything as 500.
    """

    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Backend-Service returned {status_code}: {body!r}")


class BanAdminClient:
    """Thin async client for Backend-Service's banned-fingerprints CRUD.

    Args:
        base_url: BS endpoint base URL (no trailing slash required). Example:
            ``https://api.wxyc.org/internal/banned-fingerprints``.
        http_client: Shared :class:`httpx.AsyncClient` (same singleton used by
            the lookup client).
        internal_key: Value of ``ROM_INTERNAL_KEY`` on the BS side. Sent as
            the ``X-Internal-Key`` header on every request.
        timeout: Per-call timeout in seconds. BS admin endpoints are simple
            single-row CRUD against a small table; 10s is generous.
    """

    def __init__(
        self,
        base_url: str,
        http_client: httpx.AsyncClient,
        *,
        internal_key: str,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.http_client = http_client
        self.internal_key = internal_key
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"X-Internal-Key": self.internal_key}

    @staticmethod
    def _decode_body(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return response.text

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        """Decode a 2xx body, raising BanAdminClientError if it isn't JSON.

        BS contract is JSON-on-success, but a reverse proxy or content-type
        drift could return a 200 with HTML/text. Without this guard the bare
        ``response.json()`` would raise ``json.JSONDecodeError`` and escape
        the router's ``except BanAdminClientError`` as an unhandled 500.
        """
        try:
            return response.json()
        except ValueError as exc:
            raise BanAdminClientError(
                response.status_code,
                {"error": "non_json_upstream_body", "text": response.text[:200]},
            ) from exc

    async def ban(
        self,
        *,
        fingerprint: str,
        reason: str,
        expires_in_seconds: int | None = None,
        banned_by_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a ban (BS idempotent upsert).

        Returns the upserted row as a dict. The BS endpoint validates the
        fingerprint UUID shape, the non-empty reason, and the positive
        ``expiresInSeconds`` — invalid input surfaces as a 400 wrapped in
        :class:`BanAdminClientError` rather than silently succeeding.
        """
        payload: dict[str, Any] = {"fingerprint": fingerprint, "reason": reason}
        if expires_in_seconds is not None:
            payload["expiresInSeconds"] = expires_in_seconds
        if banned_by_user_id is not None:
            payload["bannedByUserId"] = banned_by_user_id

        try:
            response = await self.http_client.post(
                self.base_url,
                json=payload,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise BanAdminClientError(
                0, {"error": "upstream_unreachable", "detail": str(exc)}
            ) from exc
        if not 200 <= response.status_code < 300:
            raise BanAdminClientError(response.status_code, self._decode_body(response))
        return self._safe_json(response)  # type: ignore[no-any-return]

    async def unban(self, *, fingerprint: str) -> None:
        """Delete a ban. Idempotent — succeeds whether or not the row existed.

        Returns None. Raises :class:`BanAdminClientError` for any non-204
        response (including 400 for a malformed fingerprint).

        ``fingerprint`` is URL-quoted so a caller that bypasses the FastAPI
        path-param validator (e.g. the Slack-native router #152 consuming an
        interaction payload) cannot inject ``?``/``#``/``/`` into the BS
        request path.
        """
        try:
            response = await self.http_client.delete(
                f"{self.base_url}/{quote(fingerprint, safe='')}",
                headers=self._headers(),
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise BanAdminClientError(
                0, {"error": "upstream_unreachable", "detail": str(exc)}
            ) from exc
        if response.status_code != 204:
            raise BanAdminClientError(response.status_code, self._decode_body(response))

    async def list_bans(
        self,
        *,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List bans (keyset-paginated). Returns ``{items, nextCursor}``.

        Both query params are optional and forwarded verbatim — BS defaults
        ``limit=50`` and rejects values outside ``[1, 200]``.
        """
        params: dict[str, str] = {}
        if limit is not None:
            params["limit"] = str(limit)
        if cursor is not None:
            params["cursor"] = cursor

        try:
            response = await self.http_client.get(
                self.base_url,
                params=params,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise BanAdminClientError(
                0, {"error": "upstream_unreachable", "detail": str(exc)}
            ) from exc
        if not 200 <= response.status_code < 300:
            raise BanAdminClientError(response.status_code, self._decode_body(response))
        return self._safe_json(response)  # type: ignore[no-any-return]
