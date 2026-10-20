"""Retry breadcrumbs for Groq AI parse calls.

``install_groq_retry_breadcrumbs`` installs a logging.Filter on the
``groq._base_client`` logger that converts ``Retrying request to <path>
in <n> seconds`` log lines into structured breadcrumbs (category
``groq.retry``). This surfaces retry cadence on captured events even
when the surrounding trace is unsampled.
"""

from __future__ import annotations

import logging
import re

import sentry_sdk
from wxyc_fastapi.observability import add_breadcrumb

logger = logging.getLogger(__name__)

GROQ_RETRY_LOGGER = "groq._base_client"

# Matches the Groq SDK retry log: "Retrying request to /openai/v1/chat/completions in 8.000000 seconds"
# Source: https://github.com/groq/groq-python/blob/main/src/groq/_base_client.py
_RETRY_LOG_RE = re.compile(r"Retrying request to (\S+) in ([\d.]+) seconds")


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
            add_breadcrumb(category="groq.retry", message=message)
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
