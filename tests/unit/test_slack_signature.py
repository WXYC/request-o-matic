"""Unit tests for services/slack_signature.py -- Slack request-signature
verification (request-o-matic#152).

Slack's own spec: https://api.slack.com/authentication/verifying-requests-from-slack
``v0=`` + HMAC-SHA256(signing_secret, f"v0:{timestamp}:{body}") over the raw
request body, plus a timestamp freshness check to blunt replay.
"""

from __future__ import annotations

import hashlib
import hmac

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
