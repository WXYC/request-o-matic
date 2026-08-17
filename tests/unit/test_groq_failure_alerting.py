"""Unit tests for how Groq parse failures are graded for alerting.

On 2026-08-17 Groq decommissioned the pinned model. Every parse 404'd, the
service fell back to `parsing_unavailable`, and the condition ran for hours
before a human noticed by running the `lookup` CLI by hand.

The post-mortem's surprise was that no signal was missing. Sentry recorded the
404s (`services.parser` logs at ERROR, and Sentry's LoggingIntegration promotes
ERROR to an event), the project's issue-alert rule fired and emailed, and every
degraded request posted a "_Parsing unavailable_" note into the Slack channel.
Three signals, all firing, none acted on -- because none of them *distinguished*
a pin that will never work again from the free tier's routine 429s. An operator
who has learned that "Groq failed" means "try again in a minute" has learned to
ignore exactly the alert that mattered.

So the grading, not the plumbing, is what this module pins:

- **Transient** -- rate limit, timeout, connection error, Groq 5xx. The pin is
  fine and this request was unlucky. WARNING, which Sentry keeps as a
  breadcrumb and never raises to an event.
- **Permanent** -- everything else. No request will succeed until a human
  changes something. ERROR with `exc_info`, which becomes a single stack-traced
  Sentry event naming the dead pin.

Unrecognized exception types grade as permanent deliberately. The failure that
started this was one nobody had enumerated in advance; an allowlist of
known-transient errors fails loud on the next unknown, whereas an allowlist of
known-permanent ones would fail silent exactly the way this incident did.

Grading changes severity only. The listener-facing contract -- HTTP 200, a
`parsing_unavailable` body, and the raw message posted to Slack -- is identical
either way, and is asserted here so a future change to the alerting cannot
quietly turn a degraded response into an error one.
"""

import logging
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastapi import FastAPI
from groq import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)
from httpx import ASGITransport, AsyncClient

from core.dependencies import (
    get_groq_client,
    get_lookup_client,
    get_posthog_client,
    get_slack_service,
)
from routers.request import router
from services.lookup_client import LookupServiceClient
from services.parser import GROQ_MODEL

_GROQ_REQUEST = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")


def _status_error(cls, status: int, message: str):
    return cls(message, response=httpx.Response(status, request=_GROQ_REQUEST), body=None)


# The 404 that actually happened, reproduced verbatim from the Sentry event.
MODEL_DECOMMISSIONED = _status_error(
    NotFoundError,
    404,
    "Error code: 404 - {'error': {'message': 'The model `llama-3.1-8b-instant` does not "
    "exist or you do not have access to it.', 'type': 'invalid_request_error', "
    "'code': 'model_not_found'}}",
)

PERMANENT_ERRORS = [
    MODEL_DECOMMISSIONED,
    _status_error(AuthenticationError, 401, "Invalid API Key"),
    _status_error(PermissionDeniedError, 403, "You do not have access to this model"),
    _status_error(BadRequestError, 400, "unsupported parameter: reasoning_effort"),
    # Not a groq type at all: the parser raises this when Groq answers with
    # something that isn't JSON, and any future unanticipated failure lands here
    # too. Permanent is the fail-loud default.
    ValueError("Invalid JSON response from Groq"),
    RuntimeError("something nobody enumerated"),
]
PERMANENT_IDS = [
    "model_decommissioned_404",
    "bad_api_key_401",
    "no_model_access_403",
    "bad_request_400",
    "value_error",
    "unknown_exception",
]

TRANSIENT_ERRORS = [
    _status_error(RateLimitError, 429, "Rate limit reached for model"),
    _status_error(InternalServerError, 503, "Service Unavailable"),
    APITimeoutError(request=_GROQ_REQUEST),
    APIConnectionError(message="Connection error.", request=_GROQ_REQUEST),
]
TRANSIENT_IDS = ["rate_limit_429", "groq_5xx", "timeout", "connection_error"]


def _make_app(slack_service: AsyncMock | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_groq_client] = lambda: Mock()
    app.dependency_overrides[get_slack_service] = lambda: slack_service
    app.dependency_overrides[get_posthog_client] = lambda: None
    app.dependency_overrides[get_lookup_client] = lambda: AsyncMock(spec=LookupServiceClient)
    return app


async def _post_request(exc: BaseException, slack_service: AsyncMock | None = None):
    """Drive one `/request` whose parse raises `exc`, returning the response."""
    app = _make_app(slack_service)
    with patch("routers.request.parse_request", new_callable=AsyncMock, side_effect=exc):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.post(
                "/api/v1/request",
                json={"message": "vi scose poise, autechre", "skip_slack": slack_service is None},
            )


def _records_at(caplog, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == level and r.name == "routers.request"]


@pytest.fixture(autouse=True)
def _capture_router_logs(caplog):
    caplog.set_level(logging.DEBUG, logger="routers.request")
    return caplog


class TestPermanentFailuresAlert:
    """A failure no retry can fix must reach Sentry as an event."""

    @pytest.mark.parametrize("exc", PERMANENT_ERRORS, ids=PERMANENT_IDS)
    @pytest.mark.asyncio
    async def test_logs_at_error(self, caplog, exc):
        """ERROR is the whole mechanism: Sentry's LoggingIntegration defaults to
        `event_level=ERROR`, so this level -- and only this level -- becomes an
        event rather than a breadcrumb."""
        await _post_request(exc)

        assert _records_at(caplog, logging.ERROR), (
            f"{type(exc).__name__} produced no ERROR record, so Sentry would "
            f"only ever see a breadcrumb"
        )

    @pytest.mark.parametrize("exc", PERMANENT_ERRORS, ids=PERMANENT_IDS)
    @pytest.mark.asyncio
    async def test_carries_exc_info_for_a_stack_trace(self, caplog, exc):
        """Without `exc_info` Sentry groups on the formatted string, so the 404's
        embedded model name splits one outage across as many issues as there are
        message variants."""
        await _post_request(exc)

        errors = _records_at(caplog, logging.ERROR)
        assert errors and all(r.exc_info is not None for r in errors)

    @pytest.mark.asyncio
    async def test_names_the_model_pin(self, caplog):
        """The operator's next action is to check the pin against Groq's live
        model list, so the pin has to be in the alert they are reading."""
        await _post_request(MODEL_DECOMMISSIONED)

        assert GROQ_MODEL in "".join(r.getMessage() for r in _records_at(caplog, logging.ERROR))


class TestTransientFailuresStayQuiet:
    """Routine free-tier turbulence must not compete with the real signal."""

    @pytest.mark.parametrize("exc", TRANSIENT_ERRORS, ids=TRANSIENT_IDS)
    @pytest.mark.asyncio
    async def test_does_not_log_at_error(self, caplog, exc):
        await _post_request(exc)

        assert not _records_at(caplog, logging.ERROR), (
            f"{type(exc).__name__} self-heals; raising it to ERROR is what "
            f"trains an operator to ignore the alert that matters"
        )

    @pytest.mark.parametrize("exc", TRANSIENT_ERRORS, ids=TRANSIENT_IDS)
    @pytest.mark.asyncio
    async def test_still_logs_at_warning(self, caplog, exc):
        """Quiet in Sentry, not invisible: the breadcrumb is what explains a
        slow request in the trace, and a 429 storm still has to be greppable."""
        await _post_request(exc)

        assert _records_at(caplog, logging.WARNING)


class TestParserDefersSeverityToItsCaller:
    """`services.parser` must not pre-empt the grading above.

    The router's decision only reaches Sentry if nothing downstream has already
    reported the same failure at ERROR. `parse_request` used to log every
    exception at ERROR before re-raising, which meant a routine 429 minted a
    Sentry event from `services.parser` no matter how carefully the router
    graded it -- the exact noise the grading exists to remove. A parser is a
    library; which failures are worth waking someone for is the caller's policy,
    and both callers (`/request` here, `/parse` in routers/parse.py) now set it
    explicitly.

    These drive the real `parse_request` rather than patching it, because the
    router-level tests above cannot see this logger at all.
    """

    @pytest.mark.parametrize("exc", TRANSIENT_ERRORS, ids=TRANSIENT_IDS)
    @pytest.mark.asyncio
    async def test_transient_groq_failure_is_not_logged_at_error(self, caplog, exc):
        from services.parser import parse_request as real_parse_request

        caplog.set_level(logging.DEBUG, logger="services.parser")
        client = Mock()
        client.chat.completions.create = AsyncMock(side_effect=exc)

        with pytest.raises(type(exc)):
            await real_parse_request("vi scose poise, autechre", client)

        parser_errors = [
            r for r in caplog.records if r.levelno == logging.ERROR and r.name == "services.parser"
        ]
        assert not parser_errors, (
            f"services.parser raised {type(exc).__name__} to ERROR, which becomes a "
            f"Sentry event regardless of how the router grades it"
        )

    @pytest.mark.asyncio
    async def test_failure_is_still_recorded_at_warning(self, caplog):
        """Deferring severity is not the same as staying silent -- the parser is
        still the only place that knows the call failed mid-flight."""
        from services.parser import parse_request as real_parse_request

        caplog.set_level(logging.DEBUG, logger="services.parser")
        client = Mock()
        client.chat.completions.create = AsyncMock(side_effect=TRANSIENT_ERRORS[0])

        with pytest.raises(RateLimitError):
            await real_parse_request("vi scose poise, autechre", client)

        assert [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and r.name == "services.parser"
        ]


class TestListenerContractIsUnchangedByGrading:
    """Severity is an operator concern; the listener must not be able to tell."""

    @pytest.mark.parametrize(
        "exc",
        PERMANENT_ERRORS + TRANSIENT_ERRORS,
        ids=PERMANENT_IDS + TRANSIENT_IDS,
    )
    @pytest.mark.asyncio
    async def test_degrades_rather_than_erroring(self, exc):
        response = await _post_request(exc)

        assert response.status_code == 200
        body = response.json()
        assert body["degraded_mode"] == "parsing_unavailable"
        assert body["parsed"] is None

    @pytest.mark.parametrize(
        "exc",
        PERMANENT_ERRORS + TRANSIENT_ERRORS,
        ids=PERMANENT_IDS + TRANSIENT_IDS,
    )
    @pytest.mark.asyncio
    async def test_raw_message_still_reaches_slack(self, exc):
        slack_service = AsyncMock()
        slack_service.post_blocks = AsyncMock()

        await _post_request(exc, slack_service)

        slack_service.post_blocks.assert_awaited_once()
