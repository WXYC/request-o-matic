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
    or non-numeric timestamp, a missing, malformed, or non-ASCII signature, or
    a timestamp outside ``max_age_seconds`` of ``now`` all return False rather
    than raising -- the router turns any False into a flat 401, no partial
    trust and nothing an attacker can tell apart from an ordinary bad
    signature.

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

    # Compare as integers. `int(timestamp)` happily accepts any digit string up
    # to CPython's 4300-digit int-from-str cap, and mixing one of those with the
    # float from time.time() raises OverflowError ("int too large to convert to
    # float") -- which ValueError above does not catch. On an unauthenticated
    # endpoint that turns a ~400-digit timestamp header into a 500 instead of
    # the flat 401 every other malformed header gets, and every such 500 is an
    # unhandled exception that ships this frame's locals -- including
    # ``signing_secret`` -- to Sentry. Truncating `now` to whole seconds is
    # lossless for this comparison: Slack's timestamps are integer seconds and
    # the window is 300 of them.
    current_time = time.time() if now is None else now
    if abs(int(current_time) - request_time) > max_age_seconds:
        return False

    basestring = f"v0:{timestamp}:".encode() + body
    digest = hmac.new(signing_secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    expected_signature = f"v0={digest}".encode("ascii")

    # Compare bytes, not str. Starlette decodes headers as latin-1, so the
    # attacker-controlled X-Slack-Signature can hold any byte >= 0x80, and
    # hmac.compare_digest raises TypeError on str operands that aren't pure
    # ASCII -- which on an unauthenticated endpoint turns a forged header into
    # a 500 (and a Sentry event) instead of the 401 every other bad signature
    # gets. latin-1 is the exact inverse of that decode, so it recovers the
    # header's bytes as sent; a value it can't encode never came off the wire
    # and could not have matched an ASCII hex digest anyway. compare_digest
    # stays constant-time over the bytes.
    try:
        provided_signature = signature.encode("latin-1")
    except UnicodeEncodeError:
        return False

    return hmac.compare_digest(expected_signature, provided_signature)
