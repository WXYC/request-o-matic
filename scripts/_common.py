"""Shared constants and utilities for CLI scripts."""

import logging
from typing import Any

PROD_URL = "https://request-o-matic-production.up.railway.app/api/v1"
STAGING_URL = "https://request-o-matic-staging.up.railway.app/api/v1"
LOCAL_URL = "http://localhost:8000/api/v1"

# Operator-facing explanation for each value of the response's `degraded_mode`
# field. Keyed on the field the server actually sets rather than inferred from a
# null `parsed`, so `search_unavailable` gets a real diagnosis too -- an LML
# outage previously rendered in both CLIs as a bare "no results", i.e. "not in
# the library", which is the wrong thing to tell an operator.
#
# The keys mirror DEGRADED_PARSING / DEGRADED_SEARCH in routers/request.py.
# Importing those directly would drag FastAPI and the Groq SDK into a CLI's
# startup, so they are duplicated here and pinned by
# tests/unit/test_common_degraded.py, which fails if the two drift.
DEGRADED_EXPLANATIONS = {
    "parsing_unavailable": (
        "Parsing unavailable -- the server could not parse this message.\n"
        "Groq is failing, so the raw message is posted to Slack unenriched\n"
        "and no library search runs. Check the service logs for the Groq error."
    ),
    "search_unavailable": (
        "Search unavailable -- the message parsed, but the library was not searched.\n"
        "The lookup service (LML) is unreachable or unconfigured, so results are\n"
        "absent rather than empty. Check LML's health and LOOKUP_SERVICE_URL."
    ),
}


def describe_degraded_mode(data: dict[str, Any]) -> str | None:
    """Return an operator-facing explanation, or None when nothing is degraded.

    Args:
        data: A decoded `/request` response body.

    Returns:
        The explanation for `data["degraded_mode"]`, or None when the field is
        absent or null. An unrecognized mode still yields a message rather than
        None, so a newly-added degraded mode is never silently rendered as a
        healthy response.
    """
    mode = data.get("degraded_mode")
    if not mode:
        return None
    return DEGRADED_EXPLANATIONS.get(
        mode, f"Service degraded ({mode}) -- see the service logs for details."
    )


def indent(text: str, prefix: str = "  ") -> str:
    """Prefix every line of `text`, so callers own their own indentation."""
    return "\n".join(f"{prefix}{line}" for line in text.splitlines())


def set_up_logging(verbose: bool, default_level: int = logging.INFO) -> None:
    """Configure logging based on verbosity level.

    Args:
        verbose: If True, use DEBUG level; otherwise use default_level.
        default_level: Logging level when not verbose (default: INFO).
    """
    level = logging.DEBUG if verbose else default_level
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler()],
    )
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
