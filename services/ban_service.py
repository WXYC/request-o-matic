"""Shared ban-mutation surface for request-o-matic.

Both the HTTP admin router (`routers/admin.py`, #151) and the future
Slack-native ban router (`routers/slack_interactivity.py`, #152) call into the
functions defined here. The motivation for the explicit indirection is that
#152 needs to apply identical validation, idempotency, and audit-actor handling
as #151 — two parallel codepaths would diverge.

There is no local state. Every call delegates to
:class:`services.ban_admin_client.BanAdminClient` which talks to BS's
`/internal/banned-fingerprints` CRUD (BS#1261). Backend-Service owns the
storage; rom owns the operator UX.

The ``actor`` argument is forwarded to BS as ``banned_by_user_id`` for the
audit trail. The HTTP admin caller passes ``None`` because they're only
identified by ``ADMIN_TOKEN`` — leaving it ``None`` is preferable to inventing
a synthetic user_id (which couldn't satisfy BS's foreign-key constraint to
``auth_user.id``). The Slack caller will pass the Slack user's mapped
``auth_user.id``.
"""

from __future__ import annotations

import logging
from typing import Any

from services.ban_admin_client import BanAdminClient

logger = logging.getLogger(__name__)


async def ban(
    client: BanAdminClient,
    *,
    fingerprint: str,
    reason: str,
    actor: str | None = None,
    expires_in_seconds: int | None = None,
) -> dict[str, Any]:
    """Ban a fingerprint. Idempotent — re-banning returns the updated row.

    Args:
        client: BS admin client (constructed by the FastAPI dependency).
        fingerprint: The iOS-Keychain UUID identifying the listener device.
            BS validates the UUID shape and rejects malformed values with 400.
        reason: Free-text reason surfaced in operator UIs. BS caps at 1000
            characters and rejects empty/whitespace.
        actor: ``auth_user.id`` of the operator initiating the ban, or
            ``None`` for HTTP-admin callers identified only by
            ``ADMIN_TOKEN``. Forwarded as ``banned_by_user_id``.
        expires_in_seconds: Optional auto-expiry. ``None`` means permanent
            until manually unbanned.

    Returns:
        The upserted row as a dict: ``fingerprint``, ``banned_at``,
        ``ban_reason``, ``ban_expires_at``, ``banned_by_user_id``.
    """
    # Fingerprint is a stable per-device identifier (iOS-Keychain UUID).
    # Log only the first 8 chars so Railway/Sentry log retention doesn't act as
    # a deanon vector for anyone with log access.
    logger.info(
        "ban_service.ban fingerprint=%s... reason_len=%d actor=%s expires_in=%s",
        fingerprint[:8],
        len(reason),
        actor or "<none>",
        expires_in_seconds,
    )
    return await client.ban(
        fingerprint=fingerprint,
        reason=reason,
        expires_in_seconds=expires_in_seconds,
        banned_by_user_id=actor,
    )


async def unban(
    client: BanAdminClient,
    *,
    fingerprint: str,
    actor: str | None = None,
) -> None:
    """Remove a ban. Idempotent — succeeds whether or not the row existed.

    The ``actor`` argument is currently used only for logging; BS's DELETE
    endpoint does not record who removed a ban. (If we ever want that audit
    trail it goes in the BS-side route as a separate column; rom should not
    invent local audit state.)
    """
    # See ban() above for the rationale on truncating the fingerprint.
    logger.info(
        "ban_service.unban fingerprint=%s... actor=%s",
        fingerprint[:8],
        actor or "<none>",
    )
    await client.unban(fingerprint=fingerprint)


async def list_bans(
    client: BanAdminClient,
    *,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """List bans. Pure pass-through to BS; no actor field (read-only)."""
    return await client.list_bans(limit=limit, cursor=cursor)
