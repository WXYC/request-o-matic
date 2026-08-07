"""Slack request-signature verification (request-o-matic#152).

``routers/slack_interactivity.py`` calls :func:`verify_slack_signature` on
every inbound ``POST /slack/interactivity`` callback *before* parsing or
trusting any part of the payload -- an unverified callback is forgeable, and
this endpoint bans people. Implemented with raw ``hmac``/``hashlib`` rather
than ``slack_sdk``: the SDK's surface dwarfs the one HMAC comparison and two
API calls (``views.open``, ``chat.update``) this ticket needs, and
``core/dependencies.SlackService`` already talks to Slack over the shared
``httpx.AsyncClient`` without it.

Spec: https://api.slack.com/authentication/verifying-requests-from-slack
"""

from __future__ import annotations

import hashlib
import hmac
import time

__all__ = ["verify_slack_signature"]

# Slack's own recommendation: reject anything older than 5 minutes to blunt
# replay of a captured request.
DEFAULT_MAX_AGE_SECONDS = 300


def verify_slack_signature(
    signing_secret: str | None,
    *,
    timestamp: str | None,
    body: bytes,
    signature: str | None,
    now: float | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> bool:
    """Verify a Slack ``X-Slack-Signature`` header against the raw request body.

    Fails closed on every ambiguous input: a missing signing secret, a missing
    or non-numeric timestamp, a missing signature, or a timestamp outside
    ``max_age_seconds`` of ``now`` all return False rather than raising --
    the router turns any False into a flat 401, no partial trust.

    Args:
        signing_secret: ``SLACK_SIGNING_SECRET``. None (unconfigured) always
            fails closed rather than skipping verification.
        timestamp: Raw ``X-Slack-Request-Timestamp`` header value.
        body: The raw request body bytes, exactly as received -- the
            signature covers the byte-for-byte payload, so this must not be
            re-serialized JSON or a re-encoded form body.
        signature: Raw ``X-Slack-Signature`` header value, e.g. ``"v0=abcd..."``.
        now: Current Unix time. Injectable for tests; defaults to
            ``time.time()``.
        max_age_seconds: Replay window. Slack recommends 300s (5 minutes).

    Returns:
        True iff the signature is valid and the timestamp is within the
        allowed age of ``now`` (in either direction, bounding clock skew).
    """
    if not signing_secret or not timestamp or not signature:
        return False

    try:
        request_time = int(timestamp)
    except ValueError:
        return False

    current_time = time.time() if now is None else now
    if abs(current_time - request_time) > max_age_seconds:
        return False

    basestring = f"v0:{timestamp}:".encode() + body
    digest = hmac.new(signing_secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    expected_signature = f"v0={digest}"

    return hmac.compare_digest(expected_signature, signature)
