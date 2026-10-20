"""Tests for Groq tracing instrumentation.

The ``groq._base_client`` logging filter converts the Groq SDK's
"Retrying request to ... in N seconds" log line into a structured
breadcrumb.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest


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

    def test_retry_log_also_emits_posthog_breadcrumb(self):
        from core.groq_tracing import install_groq_retry_breadcrumbs

        install_groq_retry_breadcrumbs()
        message = "Retrying request to /openai/v1/chat/completions in 8.000000 seconds"

        with patch("core.groq_tracing.add_breadcrumb") as mock_add:
            logging.getLogger("groq._base_client").info(message)

        mock_add.assert_called_once_with(category="groq.retry", message=message)

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
