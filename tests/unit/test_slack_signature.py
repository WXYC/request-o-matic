"""Unit tests for services/slack_signature.py -- Slack request-signature
verification (request-o-matic#152).

Slack's own spec: https://api.slack.com/authentication/verifying-requests-from-slack
``v0=`` + HMAC-SHA256(signing_secret, f"v0:{timestamp}:{body}") over the raw
request body, plus a timestamp freshness check to blunt replay.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from services.slack_signature import verify_slack_signature

SECRET = "test-signing-secret"


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    basestring = f"v0:{timestamp}:".encode() + body
    digest = hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    return f"v0={digest}"


class TestValidSignature:
    def test_accepts_correctly_signed_request(self):
        timestamp = "1700000000"
        body = b'payload={"type":"block_actions"}'
        signature = _sign(SECRET, timestamp, body)

        assert (
            verify_slack_signature(
                SECRET,
                timestamp=timestamp,
                body=body,
                signature=signature,
                now=1700000000,
            )
            is True
        )

    def test_accepts_signature_within_max_age(self):
        timestamp = "1700000000"
        body = b"payload=x"
        signature = _sign(SECRET, timestamp, body)

        assert (
            verify_slack_signature(
                SECRET,
                timestamp=timestamp,
                body=body,
                signature=signature,
                now=1700000000 + 299,
                max_age_seconds=300,
            )
            is True
        )


class TestInvalidSignature:
    def test_rejects_wrong_signature(self):
        timestamp = "1700000000"
        body = b"payload=x"

        assert (
            verify_slack_signature(
                SECRET,
                timestamp=timestamp,
                body=body,
                signature="v0=deadbeef",
                now=1700000000,
            )
            is False
        )

    def test_rejects_when_body_tampered_after_signing(self):
        timestamp = "1700000000"
        signature = _sign(SECRET, timestamp, b"payload=original")

        assert (
            verify_slack_signature(
                SECRET,
                timestamp=timestamp,
                body=b"payload=tampered",
                signature=signature,
                now=1700000000,
            )
            is False
        )

    def test_rejects_signature_from_wrong_secret(self):
        timestamp = "1700000000"
        body = b"payload=x"
        signature = _sign("a-different-secret", timestamp, body)

        assert (
            verify_slack_signature(
                SECRET,
                timestamp=timestamp,
                body=body,
                signature=signature,
                now=1700000000,
            )
            is False
        )

    def test_rejects_missing_v0_prefix(self):
        timestamp = "1700000000"
        body = b"payload=x"
        basestring = f"v0:{timestamp}:".encode() + body
        digest = hmac.new(SECRET.encode("utf-8"), basestring, hashlib.sha256).hexdigest()

        assert (
            verify_slack_signature(
                SECRET,
                timestamp=timestamp,
                body=body,
                signature=digest,  # missing "v0=" prefix
                now=1700000000,
            )
            is False
        )


class TestMalformedSignatureHeader:
    """A malformed X-Slack-Signature must be an ordinary False, never a raise.

    Starlette decodes request headers as latin-1, so every byte an attacker
    puts in X-Slack-Signature arrives as a str codepoint in U+0000..U+00FF.
    ``hmac.compare_digest`` refuses non-ASCII str operands with TypeError, so
    comparing as str turned a one-byte change to a forged header into an
    unauthenticated 500 on ``POST /slack/interactivity`` (Sentry-capturing,
    unrate-limited) instead of the flat 401 the route documents.
    """

    def test_rejects_non_ascii_signature_without_raising(self):
        timestamp = "1700000000"
        body = b"payload=x"

        assert (
            verify_slack_signature(
                SECRET,
                timestamp=timestamp,
                body=body,
                signature="v0=\xff" + "0" * 63,
                now=1700000000,
            )
            is False
        )

    def test_rejects_signature_above_the_latin1_range(self):
        """Not reachable over HTTP (headers decode as latin-1), but a direct
        caller can still hand us a codepoint > U+00FF -- fail closed rather
        than let UnicodeEncodeError escape."""
        assert (
            verify_slack_signature(
                SECRET,
                timestamp="1700000000",
                body=b"payload=x",
                signature="v0=☃" + "0" * 63,
                now=1700000000,
            )
            is False
        )

    def test_rejects_empty_signature(self):
        assert (
            verify_slack_signature(
                SECRET,
                timestamp="1700000000",
                body=b"payload=x",
                signature="",
                now=1700000000,
            )
            is False
        )

    def test_rejects_wrong_length_signature(self):
        assert (
            verify_slack_signature(
                SECRET,
                timestamp="1700000000",
                body=b"payload=x",
                signature="v0=abc",
                now=1700000000,
            )
            is False
        )


class TestFailClosedConditions:
    def test_rejects_when_signing_secret_unset(self):
        """No SLACK_SIGNING_SECRET configured -> reject everything, never skip
        verification. A misconfigured deploy must disable the endpoint, not
        open it up."""
        timestamp = "1700000000"
        body = b"payload=x"
        signature = _sign(SECRET, timestamp, body)

        assert (
            verify_slack_signature(
                None,
                timestamp=timestamp,
                body=body,
                signature=signature,
                now=1700000000,
            )
            is False
        )

    def test_rejects_missing_timestamp(self):
        body = b"payload=x"
        signature = _sign(SECRET, "1700000000", body)

        assert (
            verify_slack_signature(
                SECRET,
                timestamp=None,
                body=body,
                signature=signature,
                now=1700000000,
            )
            is False
        )

    def test_rejects_missing_signature(self):
        assert (
            verify_slack_signature(
                SECRET,
                timestamp="1700000000",
                body=b"payload=x",
                signature=None,
                now=1700000000,
            )
            is False
        )

    def test_rejects_non_numeric_timestamp(self):
        body = b"payload=x"
        signature = _sign(SECRET, "not-a-number", body)

        assert (
            verify_slack_signature(
                SECRET,
                timestamp="not-a-number",
                body=body,
                signature=signature,
                now=1700000000,
            )
            is False
        )

    @pytest.mark.parametrize(
        "timestamp",
        [
            "9" * 400,
            "9" * 4299,
            "-" + "9" * 400,
            "9" * 5000,
        ],
        ids=["overflows-float", "at-int-str-cap", "negative-overflow", "past-int-str-cap"],
    )
    def test_rejects_enormous_numeric_timestamp_without_raising(self, timestamp):
        """A digit string too large for a float must fail closed, not raise.

        ``int(timestamp)`` accepts any digit string up to CPython's 4300-digit
        int-from-str cap, and comparing one of those against ``time.time()``'s
        float raises OverflowError -- which the ValueError guard does not
        catch. On an unauthenticated endpoint that is a 500 instead of the flat
        401 every other malformed header gets, and an unhandled 500 ships the
        frame's locals (including the signing secret) to Sentry.
        """
        body = b"payload=x"

        assert (
            verify_slack_signature(
                SECRET,
                timestamp=timestamp,
                body=body,
                signature=_sign(SECRET, timestamp, body),
                now=1700000000,
            )
            is False
        )


class TestReplayProtection:
    def test_rejects_stale_timestamp(self):
        """A timestamp older than max_age_seconds is rejected even with a
        byte-for-byte valid signature -- this is what blunts replay of a
        captured request."""
        timestamp = "1700000000"
        body = b"payload=x"
        signature = _sign(SECRET, timestamp, body)

        assert (
            verify_slack_signature(
                SECRET,
                timestamp=timestamp,
                body=body,
                signature=signature,
                now=1700000000 + 301,
                max_age_seconds=300,
            )
            is False
        )

    def test_rejects_future_timestamp_beyond_max_age(self):
        """Clock skew in the other direction is bounded too, not just staleness."""
        timestamp = "1700000000"
        body = b"payload=x"
        signature = _sign(SECRET, timestamp, body)

        assert (
            verify_slack_signature(
                SECRET,
                timestamp=timestamp,
                body=body,
                signature=signature,
                now=1700000000 - 301,
                max_age_seconds=300,
            )
            is False
        )
