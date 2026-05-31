"""End-to-end ban-enforcement smoke tests against a deployed BS environment.

These exercise the actual BS ``POST /auth/check-request-ban`` endpoint
(WXYC/Backend-Service#1261). They are deliberately small — full coverage lives
in the unit suite. The point here is to catch contract drift between ROM's
``BanCheckClient`` and the live BS endpoint.

Run only against staging, since they POST to the deployed ROM ``/request``
endpoint (which posts to Slack from the deployed container — production runs
would spam the live WXYC channel; we always pass ``skip_slack=True`` for
safety, but staging is the conventional target).

Activation:

* Marker: ``external_api``
* Required env: ``BS_CHECK_REQUEST_BAN_URL`` (full staging URL), and ROM
  staging must have ``ENFORCE_REQUEST_BANS=true`` set for the in-router
  enforcement to actually fire. Without one of those, the suite is skipped.

Run with:

    BS_CHECK_REQUEST_BAN_URL=https://wxyc-auth-staging.up.railway.app/auth/check-request-ban \\
        TEST_ENV=staging pytest tests/integration/test_ban_enforcement_e2e.py -v -m external_api
"""

from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.external_api

BS_URL: str | None = os.getenv("BS_CHECK_REQUEST_BAN_URL")

skip_if_no_bs_url = pytest.mark.skipif(
    not BS_URL,
    reason="BS_CHECK_REQUEST_BAN_URL not set — skipping live BS contract tests",
)


@skip_if_no_bs_url
@pytest.mark.asyncio
async def test_bs_check_request_ban_no_signal_returns_400():
    """Smoke test: BS endpoint exists and returns 400 no_signal when called bare.

    This is the cheapest possible contract check — it doesn't require any
    pre-seeded user or fingerprint to exist on the BS side, and it pins the
    request shape (POST, no body) and BS's error response contract.
    """
    assert BS_URL is not None  # narrows for mypy; the skipif guarantees this
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(BS_URL)

    assert response.status_code == 400, (
        f"Expected 400 no_signal from BS without headers, got {response.status_code}: "
        f"{response.text[:300]}"
    )
    body = response.json()
    assert body.get("error") == "no_signal", f"Unexpected error body: {body}"


@skip_if_no_bs_url
@pytest.mark.asyncio
async def test_bs_check_request_ban_unknown_fingerprint_returns_unbanned():
    """A random UUID fingerprint should be 200 banned=false.

    BS#1261's banned_fingerprints table is keyed on UUID. A fresh random UUID
    is overwhelmingly unlikely to collide with a real ban, so the response
    should be 200 ``banned=false`` with no userId.
    """
    import uuid

    assert BS_URL is not None  # narrows for mypy
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            BS_URL,
            headers={"X-Device-Fingerprint": str(uuid.uuid4())},
        )

    assert response.status_code == 200, (
        f"Expected 200 from BS with random fingerprint, got {response.status_code}: "
        f"{response.text[:300]}"
    )
    body = response.json()
    assert body.get("banned") is False
    assert body.get("userId") is None
