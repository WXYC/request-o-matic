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

pytestmark = [pytest.mark.external_api, pytest.mark.contract]


@pytest.fixture
def local_server():
    """Neutralize the package-level autouse local-server fixture.

    ``tests/integration/conftest.py`` boots a uvicorn process for every test in
    this directory. This module only talks to Groq, so a server it never calls
    is pure cost -- and worse, an unrelated app-boot failure would surface here
    as a spurious "the model pin is dead" alarm. Overriding the fixture at
    module scope keeps the signal clean.
    """
    return None


@pytest.mark.asyncio
async def test_pinned_model_is_still_served_by_groq() -> None:
    """`GROQ_MODEL` must appear in Groq's live model list.

    A failure here means the pin is dead and the parser is (or is about to be)
    hard-down in the `parsing_unavailable` degraded path. The fix is to
    re-point `GROQ_MODEL` at a currently-served model and re-run the NLP suite,
    not to relax this assertion.
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        pytest.skip("GROQ_API_KEY not set")

    client = AsyncGroq(api_key=api_key)
    served = {model.id for model in (await client.models.list()).data}

    assert GROQ_MODEL in served, (
        f"Pinned model {GROQ_MODEL!r} is no longer served by Groq. "
        f"The parser will 404 and degrade to parsing_unavailable. "
        f"Currently served: {sorted(served)}"
    )
