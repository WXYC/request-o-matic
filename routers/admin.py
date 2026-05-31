"""Admin HTTP API for request-line fingerprint bans (request-o-matic#151).

Three endpoints under ``/admin/bans``, all gated by
``Authorization: Bearer <ADMIN_TOKEN>``. ROM is a thin proxy — it owns no ban
state. Every write goes to Backend-Service's ``/internal/banned-fingerprints``
CRUD (BS#1261) via :mod:`services.ban_service` and :mod:`services.ban_admin_client`.

Idempotency is preserved end-to-end:

* POST a ban for an already-banned fingerprint -> 200 with the updated row.
* DELETE a ban for a non-existent fingerprint -> 204.

The future Slack-native ban router (#152) will reuse the same
:mod:`services.ban_service` functions so the two operator UXes don't drift
apart.

Operator runbook: see `docs/admin-bans.md`.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from core.dependencies import get_ban_admin_client, require_admin_token
from services import ban_service
from services.ban_admin_client import BanAdminClient, BanAdminClientError

# BS-side 4xx statuses that reflect rom->BS hop configuration or pacing failures
# rather than the operator's request itself. Forwarding these verbatim would
# confuse the operator (BS 401 != rom 401; BS 429 != rom rate-limited the operator).
# Map them all to 502 (the upstream is misbehaving from rom's perspective).
_UPSTREAM_FAULT_4XX = frozenset({401, 403, 429})


def _redact_fingerprint(fingerprint: str) -> str:
    """First 8 chars for logs/traces. The full UUID is a stable per-device
    identifier and shouldn't sit in plaintext in long-retention sinks."""
    return f"{fingerprint[:8]}..."


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/bans", tags=["admin"])


class BanCreateRequest(BaseModel):
    """Body for ``POST /admin/bans``.

    Mirrors BS's input shape but uses snake_case (rom-side convention) and
    converts to BS's ``expiresInSeconds`` camelCase at the BanAdminClient
    boundary. ``actor`` is omitted because HTTP-admin callers are identified
    only by ``ADMIN_TOKEN``; if a future surface needs to forward an actor,
    add the field then.

    ``extra='forbid'`` so operator typos like ``banned_by_user_id`` or
    ``expiresInSeconds`` (camelCase from the BS docs) surface as 422 rather
    than being silently dropped.
    """

    model_config = ConfigDict(extra="forbid")

    fingerprint: UUID = Field(
        ...,
        description=(
            "iOS-Keychain UUID identifying the listener device to ban. Any "
            "UUID version is accepted (matches BS's permissive regex)."
        ),
    )
    reason: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="Operator-visible reason. Surfaced in Slack/admin UIs.",
    )
    expires_in_seconds: int | None = Field(
        None,
        gt=0,
        description="Optional auto-expiry (seconds). Omit for a permanent ban.",
    )


def _map_upstream_error(exc: BanAdminClientError) -> HTTPException:
    """Translate a BS-side rejection into an operator-friendly HTTPException.

    * Transport failure (status 0) -> 502 ('upstream unreachable').
    * 5xx -> 502 (the upstream itself failed; this service is fine).
    * BS 401/403/429 -> 502 (these reflect the rom->BS hop, not the operator
      -> rom request; forwarding them verbatim conflates two auth/rate
      layers and misleads the operator).
    * Other 4xx (400, 404, 422, ...) -> forward verbatim so operator typos
      surface cleanly.
    """
    detail = {"upstream_status": exc.status_code, "upstream_body": exc.body}
    if exc.status_code == 0 or exc.status_code >= 500 or exc.status_code in _UPSTREAM_FAULT_4XX:
        return HTTPException(status_code=502, detail=detail)
    return HTTPException(status_code=exc.status_code, detail=detail)


@router.post(
    "",
    summary="Ban a fingerprint",
    description=(
        "Create or update a ban on the given fingerprint. Idempotent: re-banning "
        "an already-banned fingerprint returns 200 with the updated record."
    ),
    responses={
        200: {"description": "Ban created or updated"},
        400: {"description": "BS-side validation failure (forwarded verbatim)"},
        422: {
            "description": "Local validation failure (bad UUID, empty reason, expires_in_seconds<=0, or unknown field)"
        },
        401: {"description": "Missing authorization"},
        403: {"description": "Invalid or missing admin token"},
        502: {"description": "Backend-Service upstream error"},
        503: {"description": "Ban admin upstream not configured"},
    },
)
async def create_ban(
    body: BanCreateRequest = Body(...),
    _: None = Depends(require_admin_token),
    client: BanAdminClient = Depends(get_ban_admin_client),
) -> dict:
    """POST /admin/bans — create or update a ban via Backend-Service."""
    fingerprint_str = str(body.fingerprint)
    try:
        row = await ban_service.ban(
            client,
            fingerprint=fingerprint_str,
            reason=body.reason,
            actor=None,  # HTTP-admin callers have no auth_user.id
            expires_in_seconds=body.expires_in_seconds,
        )
    except BanAdminClientError as exc:
        logger.warning(
            "create_ban upstream error fingerprint=%s status=%d body=%r",
            _redact_fingerprint(fingerprint_str),
            exc.status_code,
            exc.body,
        )
        raise _map_upstream_error(exc) from exc
    return row


@router.delete(
    "/{fingerprint}",
    status_code=204,
    summary="Unban a fingerprint",
    description=(
        "Remove a ban on the given fingerprint. Idempotent: unbanning a "
        "fingerprint that isn't banned still returns 204."
    ),
    responses={
        204: {"description": "Ban removed (or never existed)"},
        422: {"description": "Path fingerprint is not a valid UUID"},
        401: {"description": "Missing authorization"},
        403: {"description": "Invalid or missing admin token"},
        502: {"description": "Backend-Service upstream error"},
        503: {"description": "Ban admin upstream not configured"},
    },
)
async def delete_ban(
    fingerprint: UUID = Path(..., description="UUID of the fingerprint to unban."),
    _: None = Depends(require_admin_token),
    client: BanAdminClient = Depends(get_ban_admin_client),
) -> Response:
    """DELETE /admin/bans/{fingerprint} — remove a ban via Backend-Service."""
    fingerprint_str = str(fingerprint)
    try:
        await ban_service.unban(client, fingerprint=fingerprint_str, actor=None)
    except BanAdminClientError as exc:
        logger.warning(
            "delete_ban upstream error fingerprint=%s status=%d body=%r",
            _redact_fingerprint(fingerprint_str),
            exc.status_code,
            exc.body,
        )
        raise _map_upstream_error(exc) from exc
    return Response(status_code=204)


@router.get(
    "",
    summary="List bans (keyset-paginated)",
    description=(
        "List currently banned fingerprints. Pagination shape matches "
        "Backend-Service: ``{items, nextCursor}``. Pass ``cursor`` from a "
        "previous response to fetch the next page; ``nextCursor`` is null on "
        "the last page."
    ),
    responses={
        200: {"description": "List of bans"},
        422: {"description": "Local validation failure (limit outside 1-200)"},
        401: {"description": "Missing authorization"},
        403: {"description": "Invalid or missing admin token"},
        502: {"description": "Backend-Service upstream error"},
        503: {"description": "Ban admin upstream not configured"},
    },
)
async def list_bans(
    limit: int | None = Query(None, ge=1, le=200, description="Max items per page (1-200)."),
    cursor: str | None = Query(None, description="Opaque pagination cursor from a prior response."),
    _: None = Depends(require_admin_token),
    client: BanAdminClient = Depends(get_ban_admin_client),
) -> dict:
    """GET /admin/bans — list bans via Backend-Service."""
    try:
        return await ban_service.list_bans(client, limit=limit, cursor=cursor)
    except BanAdminClientError as exc:
        logger.warning(
            "list_bans upstream error status=%d body=%r",
            exc.status_code,
            exc.body,
        )
        raise _map_upstream_error(exc) from exc
