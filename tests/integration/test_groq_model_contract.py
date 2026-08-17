"""Liveness contract for the pinned Groq model.

Bug context: on 2026-08-17 Groq decommissioned `llama-3.1-8b-instant` (the whole
Llama 3.x family disappeared from the account's model list). `GROQ_MODEL` is a
hardcoded constant, so every `/request` began returning 404 `model_not_found`
and the service silently fell back to `parsing_unavailable` -- posting raw,
unparsed listener messages to Slack. Nothing alerted; it surfaced only when an
operator ran the `lookup` CLI by hand roughly 14 hours later.

Every other parser test mocks the Groq client, so none of them can observe a
model being retired out from under us. This one asks Groq what it actually
serves. It is the cheapest possible check -- a single unauthenticated-shape GET
to /models, no completion tokens -- so it is safe to run on the nightly cadence.

Markers: `external_api` (needs GROQ_API_KEY + egress) and `contract` (verifies an
external API's shape rather than our behavior). Both are deselected by default
via `addopts`; run explicitly:

    pytest tests/integration/test_groq_model_contract.py -m "external_api and contract"
"""

from __future__ import annotations

import os

import pytest
from groq import AsyncGroq

from services.parser import GROQ_MODEL

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Module-level skipif rather than a runtime pytest.skip(), matching the idiom in
# test_integration.py and test_ban_enforcement_e2e.py: it decides at collection
# time, so a missing key reports as a skipped test rather than one that started.
skip_if_no_groq = pytest.mark.skipif(not GROQ_API_KEY, reason="GROQ_API_KEY not set")

pytestmark = [pytest.mark.external_api, pytest.mark.contract]


@pytest.fixture(autouse=True)
def ensure_server_running():
    """Neutralize the package-level autouse local-server fixture.

    ``tests/integration/conftest.py`` boots a uvicorn process for every test in
    this directory. This module only talks to Groq, so a server it never calls
    is pure cost -- and worse, an unrelated app-boot failure would surface here
    as a spurious "the model pin is dead" alarm.

    Shadowing ``ensure_server_running`` (rather than ``local_server``) cuts the
    dependency edge directly and leaves the session-scoped ``local_server``
    intact for anything else. Same idiom as tests/integration/test_install_lookup.py.
    """


@pytest.mark.asyncio
@skip_if_no_groq
async def test_pinned_model_is_still_served_by_groq() -> None:
    """`GROQ_MODEL` must appear in Groq's live model list.

    A failure here means the pin is dead and the parser is (or is about to be)
    hard-down in the `parsing_unavailable` degraded path. The fix is to
    re-point `GROQ_MODEL` at a currently-served model and re-run the NLP suite,
    not to relax this assertion.
    """
    client = AsyncGroq(api_key=GROQ_API_KEY)
    try:
        served = {model.id for model in (await client.models.list()).data}
    finally:
        await client.close()

    assert GROQ_MODEL in served, (
        f"Pinned model {GROQ_MODEL!r} is no longer served by Groq. "
        f"The parser will 404 and degrade to parsing_unavailable. "
        f"Currently served: {sorted(served)}"
    )
