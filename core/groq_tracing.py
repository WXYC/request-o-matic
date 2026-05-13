"""Sentry tracing helpers for Groq AI parse calls.

Two pieces of instrumentation live here:

1. ``groq_parse_span`` — context manager that opens an ``ai.parse`` span around
   a single Groq chat-completions call. Tagged with model name, input length,
   and (on success) token counts + parse-result fields. ``HttpxIntegration``
   already emits one child span per HTTP attempt, so the ``ai.parse`` span
   shows the 429-retry cadence as a series of child spans with sleep gaps
   between them, under a single named parent that consumers can filter on
   in Sentry's trace explorer.

2. ``install_groq_retry_breadcrumbs`` — installs a logging.Filter on the
   ``groq._base_client`` logger that converts ``Retrying request to <path>
   in <n> seconds`` log lines into structured Sentry breadcrumbs (category
   ``groq.retry``). This surfaces retry cadence on captured events even
   when the surrounding trace is unsampled.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Iterator

import sentry_sdk

logger = logging.getLogger(__name__)

GROQ_RETRY_LOGGER = "groq._base_client"

# Matches the Groq SDK retry log: "Retrying request to /openai/v1/chat/completions in 8.000000 seconds"
# Source: https://github.com/groq/groq-python/blob/main/src/groq/_base_client.py
_RETRY_LOG_RE = re.compile(r"Retrying request to (\S+) in ([\d.]+) seconds")


@contextlib.contextmanager
def groq_parse_span(*, model: str, message: str) -> Iterator[sentry_sdk.tracing.Span]:
    """Open an ``ai.parse`` span around a Groq chat-completions call.

    The yielded span is pre-tagged with ``ai.model`` and the input message
    length. Callers should additionally call ``span.set_data(...)`` for
    output-side fields (parsed result, token counts) after a successful
    Groq response.

    When Sentry isn't initialized, ``sentry_sdk.start_span`` returns a no-op
    span that swallows ``set_data`` silently, so this is safe to call
    unconditionally.
    """
    with sentry_sdk.start_span(op="ai.parse", name="groq.parse") as span:
        span.set_data("ai.model", model)
        span.set_data("ai.input.message_length", len(message))
        yield span


class _GroqRetryBreadcrumbFilter(logging.Filter):
    """logging.Filter that emits a Sentry breadcrumb for each retry log.

    The filter never suppresses records (``filter()`` always returns ``True``);
    it's a side-effecting hook, not a gate.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        message = record.getMessage()
        match = _RETRY_LOG_RE.search(message)
        if match is None:
            return True

        try:
            sentry_sdk.add_breadcrumb(
                category="groq.retry",
                message=message,
                level="warning",
                data={
                    "path": match.group(1),
                    "retry_in_seconds": float(match.group(2)),
                },
            )
        except Exception:
            # Breadcrumb emission must never break logging itself.
            pass
        return True


def install_groq_retry_breadcrumbs() -> None:
    """Install the retry-breadcrumb filter on ``groq._base_client``.

    Idempotent: a second call is a no-op.
    """
    target = logging.getLogger(GROQ_RETRY_LOGGER)
    for existing in target.filters:
        if isinstance(existing, _GroqRetryBreadcrumbFilter):
            return
    target.addFilter(_GroqRetryBreadcrumbFilter())
