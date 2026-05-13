"""Tests for Groq tracing instrumentation.

Two pieces are exercised here:

1. The ``ai.parse`` span opened around ``services.parser.parse_request``
   (input/output tags + token counts).
2. The ``groq._base_client`` logging filter that converts the Groq SDK's
   "Retrying request to ... in N seconds" log line into a structured
   Sentry breadcrumb.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from services.parser import parse_request


def _groq_response(
    content: dict | None = None,
    prompt_tokens: int = 100,
    completion_tokens: int = 10,
) -> Mock:
    """Build a Mock that looks like a Groq chat.completions response."""
    if content is None:
        content = {"is_request": True, "message_type": "request"}
    response = Mock()
    response.choices = [Mock()]
    response.choices[0].message = Mock()
    response.choices[0].message.content = json.dumps(content)
    response.usage = Mock()
    response.usage.prompt_tokens = prompt_tokens
    response.usage.completion_tokens = completion_tokens
    return response


class TestAiParseSpan:
    """The parser opens an `ai.parse` span around the Groq call, tags it with
    model + input metadata, and (on success) annotates it with token counts
    and the parsed result. Httpx child spans cover the actual HTTP attempts;
    this parent span is the semantic container."""

    @pytest.mark.asyncio
    async def test_opens_ai_parse_span(self):
        client = MagicMock()
        client.chat.completions.create = AsyncMock(return_value=_groq_response())

        with patch("core.groq_tracing.sentry_sdk.start_span") as mock_start_span:
            mock_span = MagicMock()
            mock_start_span.return_value.__enter__.return_value = mock_span

            await parse_request("any message", client)

            mock_start_span.assert_called_once()
            kwargs = mock_start_span.call_args.kwargs
            assert kwargs["op"] == "ai.parse"
            assert kwargs["name"] == "groq.parse"

    @pytest.mark.asyncio
    async def test_span_tagged_with_input(self):
        client = MagicMock()
        client.chat.completions.create = AsyncMock(return_value=_groq_response())

        with patch("core.groq_tracing.sentry_sdk.start_span") as mock_start_span:
            mock_span = MagicMock()
            mock_start_span.return_value.__enter__.return_value = mock_span

            await parse_request("hello world", client)

            mock_span.set_data.assert_any_call("ai.model", "llama-3.1-8b-instant")
            mock_span.set_data.assert_any_call("ai.input.message_length", len("hello world"))

    @pytest.mark.asyncio
    async def test_span_tagged_with_output_fields(self):
        client = MagicMock()
        client.chat.completions.create = AsyncMock(
            return_value=_groq_response(
                content={
                    "song": "la paradoja",
                    "artist": "Juana Molina",
                    "is_request": True,
                    "message_type": "request",
                },
                prompt_tokens=150,
                completion_tokens=20,
            )
        )

        with patch("core.groq_tracing.sentry_sdk.start_span") as mock_start_span:
            mock_span = MagicMock()
            mock_start_span.return_value.__enter__.return_value = mock_span

            await parse_request("play la paradoja by Juana Molina", client)

            mock_span.set_data.assert_any_call("ai.output.is_request", True)
            mock_span.set_data.assert_any_call("ai.output.message_type", "request")
            mock_span.set_data.assert_any_call("ai.tokens.prompt", 150)
            mock_span.set_data.assert_any_call("ai.tokens.completion", 20)

    @pytest.mark.asyncio
    async def test_span_still_opened_when_groq_raises(self):
        """Span must wrap the call even on failure so the exception is captured
        with the span context. We re-raise so the router's existing error
        handling (degraded mode, etc.) keeps working."""
        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=RuntimeError("boom"))

        with patch("core.groq_tracing.sentry_sdk.start_span") as mock_start_span:
            mock_span = MagicMock()
            mock_start_span.return_value.__enter__.return_value = mock_span

            with pytest.raises(RuntimeError):
                await parse_request("any", client)

            mock_start_span.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_output_tags_when_groq_raises(self):
        """Output tags are only set after a successful parse — never on the
        error path. Otherwise a partial set of tags could mislead a reader
        into thinking the parse succeeded."""
        client = MagicMock()
        client.chat.completions.create = AsyncMock(side_effect=RuntimeError("boom"))

        with patch("core.groq_tracing.sentry_sdk.start_span") as mock_start_span:
            mock_span = MagicMock()
            mock_start_span.return_value.__enter__.return_value = mock_span

            with pytest.raises(RuntimeError):
                await parse_request("any", client)

            output_calls = [
                c
                for c in mock_span.set_data.call_args_list
                if c.args and c.args[0].startswith("ai.output")
            ]
            assert output_calls == []


class TestGroqRetryBreadcrumbs:
    """The Groq SDK silently retries 429/5xx via `time.sleep` inside
    `_base_client`. Each retry emits a log line ``Retrying request to
    <path> in <n> seconds``. We install a logging.Filter on that logger to
    turn those lines into structured Sentry breadcrumbs so the retry cadence
    shows up on captured events even if the surrounding trace is dropped."""

    @pytest.fixture(autouse=True)
    def _clean_filters(self):
        """Strip any previously-installed retry filters between tests, and
        ensure the logger emits INFO so filters actually run (the Groq SDK
        logs retries at INFO level; production sets the root level to INFO
        via ``setup_logging``)."""
        from core.groq_tracing import _GroqRetryBreadcrumbFilter

        groq_logger = logging.getLogger("groq._base_client")
        prior_level = groq_logger.level
        groq_logger.setLevel(logging.INFO)
        groq_logger.filters = [
            f for f in groq_logger.filters if not isinstance(f, _GroqRetryBreadcrumbFilter)
        ]
        yield
        groq_logger.filters = [
            f for f in groq_logger.filters if not isinstance(f, _GroqRetryBreadcrumbFilter)
        ]
        groq_logger.setLevel(prior_level)

    def test_install_adds_filter_to_groq_base_client_logger(self):
        from core.groq_tracing import (
            _GroqRetryBreadcrumbFilter,
            install_groq_retry_breadcrumbs,
        )

        install_groq_retry_breadcrumbs()
        groq_logger = logging.getLogger("groq._base_client")
        assert any(isinstance(f, _GroqRetryBreadcrumbFilter) for f in groq_logger.filters)

    def test_install_is_idempotent(self):
        from core.groq_tracing import (
            _GroqRetryBreadcrumbFilter,
            install_groq_retry_breadcrumbs,
        )

        install_groq_retry_breadcrumbs()
        install_groq_retry_breadcrumbs()

        groq_logger = logging.getLogger("groq._base_client")
        count = sum(1 for f in groq_logger.filters if isinstance(f, _GroqRetryBreadcrumbFilter))
        assert count == 1

    def test_retry_log_emits_breadcrumb_with_structured_data(self):
        from core.groq_tracing import install_groq_retry_breadcrumbs

        install_groq_retry_breadcrumbs()
        groq_logger = logging.getLogger("groq._base_client")

        with patch("core.groq_tracing.sentry_sdk.add_breadcrumb") as mock_add:
            groq_logger.info("Retrying request to /openai/v1/chat/completions in 8.000000 seconds")

        mock_add.assert_called_once()
        kwargs = mock_add.call_args.kwargs
        assert kwargs["category"] == "groq.retry"
        assert kwargs["data"]["path"] == "/openai/v1/chat/completions"
        assert kwargs["data"]["retry_in_seconds"] == 8.0

    def test_non_retry_log_does_not_emit_breadcrumb(self):
        from core.groq_tracing import install_groq_retry_breadcrumbs

        install_groq_retry_breadcrumbs()
        groq_logger = logging.getLogger("groq._base_client")

        with patch("core.groq_tracing.sentry_sdk.add_breadcrumb") as mock_add:
            groq_logger.info("Request to /openai/v1/chat/completions succeeded")
            groq_logger.debug("Some other diagnostic message")

        mock_add.assert_not_called()

    def test_filter_returns_true_so_log_still_propagates(self):
        """The filter must never suppress the underlying log record — it's a
        breadcrumb emitter, not a log gate."""
        from core.groq_tracing import _GroqRetryBreadcrumbFilter

        f = _GroqRetryBreadcrumbFilter()
        record = logging.LogRecord(
            name="groq._base_client",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="Retrying request to /x in 1 seconds",
            args=(),
            exc_info=None,
        )
        assert f.filter(record) is True

    def test_breadcrumb_emission_failure_does_not_break_logging(self):
        """If Sentry isn't initialized or `add_breadcrumb` raises for any
        reason, the log record must still propagate normally."""
        from core.groq_tracing import _GroqRetryBreadcrumbFilter

        f = _GroqRetryBreadcrumbFilter()
        record = logging.LogRecord(
            name="groq._base_client",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="Retrying request to /x in 1 seconds",
            args=(),
            exc_info=None,
        )
        with patch(
            "core.groq_tracing.sentry_sdk.add_breadcrumb",
            side_effect=RuntimeError("sentry not ready"),
        ):
            assert f.filter(record) is True
