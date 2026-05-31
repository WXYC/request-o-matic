"""HTTP client for Backend-Service ``POST /auth/check-request-ban``.

BS#1261 exposes a public endpoint on the apps/auth service (port 8082) that
ROM calls on every ``POST /request`` to decide whether to allow or block a
listener. See WXYC/Backend-Service#1261 for the architecture record and
``apps/auth/check-request-ban-handler.ts`` in that repo for the BS-side
implementation.

Contract (relevant subset):

* Inputs travel in two request headers — ``Authorization: Bearer <jwt>`` and
  ``X-Device-Fingerprint: <uuid>``. The body is empty. At least one of the
  two must be present; BS returns 400 ``no_signal`` otherwise. We treat 400
  as a contract bug and surface it as ``BanCheckUnavailableError`` so the router
  fails open rather than silently banning every caller.
* 200 with ``banned: true`` → caller is banned. Response carries ``userId``,
  ``fingerprint``, ``banReason``, ``banSource`` (``"user"`` | ``"fingerprint"``).
* 200 with ``banned: false`` → caller is allowed.
* 401 (invalid/expired JWT) and 404 (user not found) → treat as "proceed as
  unauthenticated". The caller MUST NOT receive a 401 on ``POST /request``;
  v3.1 iOS clients in production send no Authorization header, and an invalid
  JWT (e.g. from a stale install) must not break them.
* Network errors, timeouts, and 5xx → ``BanCheckUnavailableError``. The router
  fails open: log a Sentry breadcrumb, emit ``degraded_mode=ban_check_unavailable``,
  and proceed with the request.

No ``X-Internal-Key`` header is forwarded: the BS endpoint is intentionally
public (per ``apps/auth/app.ts`` comment, "intentionally public (no
X-Internal-Key gate)"). Authentication is per-request via the JWT and/or
fingerprint, and BS bounds the cost with per-IP rate limiting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

__all__ = [
    "BanCheckClient",
    "BanCheckResult",
    "BanCheckUnavailableError",
]

logger = logging.getLogger(__name__)


class BanCheckUnavailableError(Exception):
    """Raised when the BS ban-check endpoint is unreachable or returns 5xx/400.

    The router catches this and fails open: the listener's request proceeds
    through the existing pipeline, a Sentry breadcrumb is recorded, and a
    ``degraded_mode=ban_check_unavailable`` telemetry property is emitted so
    operators can see the outage in PostHog.
    """


@dataclass(frozen=True)
class BanCheckResult:
    """Parsed result of a ban check.

    Attributes:
        banned: True when BS says block the caller.
        user_id: ``user.id`` claim from the verified JWT (None when the call
            was fingerprint-only, when the JWT was invalid, or when the user
            was not found).
        fingerprint: The echoed device fingerprint (None when absent).
        ban_reason: Free-form ban reason from BS (only set when ``banned``).
        ban_source: ``"user"`` (better-auth user ban) or ``"fingerprint"``
            (banned_fingerprints row). Only set when ``banned``.
    """

    banned: bool
    user_id: str | None = None
    fingerprint: str | None = None
    ban_reason: str | None = None
    ban_source: str | None = None


class BanCheckClient:
    """Thin async client for BS ``POST /auth/check-request-ban``.

    Args:
        url: Full URL of the BS endpoint, e.g.
            ``http://localhost:8082/auth/check-request-ban``.
        http_client: Shared ``httpx.AsyncClient`` (Sentry's httpx integration
            adds distributed tracing so the BS call appears as a child span
            of ``handle_request``).
        timeout: Per-request timeout in seconds. BS's handler does a JWT
            signature verify + 1-2 DB lookups; ~5s is plenty.
        retry_delay: Currently unused — kept as a no-op kwarg so the
            constructor signature stays stable if we add a single retry later.
    """

    _NETWORK_ERRORS = (httpx.ConnectError, httpx.TimeoutException, httpx.TransportError)

    def __init__(
        self,
        url: str,
        http_client: httpx.AsyncClient,
        *,
        timeout: float = 5.0,
        retry_delay: float = 0.0,
    ):
        self.url = url
        self.http_client = http_client
        self.timeout = timeout
        self.retry_delay = retry_delay

    async def check(
        self,
        *,
        authorization: str | None,
        fingerprint: str | None,
    ) -> BanCheckResult:
        """Call BS and return the parsed result.

        Args:
            authorization: Raw value of the caller's ``Authorization`` header
                (e.g. ``"Bearer <jwt>"``), or None.
            fingerprint: Raw value of the caller's ``X-Device-Fingerprint``
                header, or None.

        Returns:
            A ``BanCheckResult``. ``banned=False`` is also returned for the
            "proceed as unauth" cases (BS 401/404).

        Raises:
            ValueError: If neither ``authorization`` nor ``fingerprint`` is
                supplied — the caller must gate that case to avoid the
                pointless BS round-trip.
            BanCheckUnavailableError: On network errors, timeouts, 5xx, or
                contract-bug 4xx (e.g. 400 ``no_signal``).
        """
        if not authorization and not fingerprint:
            raise ValueError(
                "BanCheckClient.check requires at least one of authorization or fingerprint"
            )

        headers: dict[str, str] = {}
        if authorization:
            headers["Authorization"] = authorization
        if fingerprint:
            headers["X-Device-Fingerprint"] = fingerprint

        try:
            response = await self.http_client.post(
                self.url,
                headers=headers,
                timeout=self.timeout,
            )
        except self._NETWORK_ERRORS as exc:
            logger.warning("BS ban-check unreachable (%s: %s)", type(exc).__name__, exc)
            raise BanCheckUnavailableError(str(exc)) from exc

        status = response.status_code
        if status in (401, 404):
            # Invalid JWT or unknown user — proceed as unauthenticated.
            # iOS v3.1 clients (the current App Store binary) send no
            # Authorization header, and a stale JWT from a re-install must
            # not break them.
            logger.debug("BS ban-check returned %d; treating as proceed-as-unauth", status)
            return BanCheckResult(banned=False)

        if 500 <= status < 600:
            logger.warning("BS ban-check returned %d", status)
            raise BanCheckUnavailableError(f"BS returned {status}")

        if status != 200:
            # 400 no_signal / 400 invalid_fingerprint / unexpected 4xx.
            # Surface as unavailable so the router fails open rather than
            # turning a contract bug into a silent block-everyone failure.
            logger.warning("BS ban-check unexpected status %d: %s", status, response.text[:200])
            raise BanCheckUnavailableError(f"BS returned {status}")

        try:
            body = response.json()
        except ValueError as exc:
            logger.warning("BS ban-check returned 200 with non-JSON body")
            raise BanCheckUnavailableError("BS returned non-JSON body") from exc

        return BanCheckResult(
            banned=bool(body.get("banned", False)),
            user_id=body.get("userId"),
            fingerprint=body.get("fingerprint"),
            ban_reason=body.get("banReason"),
            ban_source=body.get("banSource"),
        )
