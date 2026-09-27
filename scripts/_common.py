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


def shelf_location(item: dict[str, Any]) -> str | None:
    """Return the shelf locator to print for one `library_results` item.

    The service composes `call_number` from genre, format and the call-number
    components, and `services/slack.py` renders that value verbatim. Both CLIs
    print what this returns, so one record names one shelf slot on every
    surface (#298).

    The composition belongs to the producer and lives here only as a legacy
    shim, never as a "richer" local format. Genre is the shelf *section*, so a
    re-derived `letters num/num` is the least specific string available: over a
    64,193-row library.db, 48.8% of rows shared it with another row against
    7.3% for the composed one, and `V/A 0/3` alone named 57 records across 12
    genres. Various-Artists rows are worst hit -- `artist_call_number` is `0`
    for all 6,318 of them, leaving genre as the only component doing any work.

    Args:
        item: One decoded `library_results` entry.

    Returns:
        The locator, or None when there is nothing to print -- callers render
        that as they render any other empty field.

        An **absent** `call_number` falls back to the components: the field is
        required by the contract, but a CLI points at whatever is deployed and
        a service predating it still has the parts. A **present** one is
        authoritative even when it is empty or null, because that means the
        producer composed nothing from the very components a fallback would
        reach for, and recomposing it here is the divergence this function
        exists to remove.

        The components are `int | None`, and their two nullish shapes are not
        interchangeable: `0` is a real Various-Artists artist number that a
        truthiness test would blank, while `None` must not reach an operator
        as the word `None`. With neither number present there is no slot to
        derive, so the letters alone are not printed as a bare `MOL /`.
    """
    if "call_number" in item:
        return item["call_number"] or None
    call_letters = item.get("call_letters")
    if not call_letters:
        return None
    artist_num = item.get("artist_call_number")
    release_num = item.get("release_call_number")
    if artist_num is None and release_num is None:
        return None
    return (
        f"{call_letters} "
        f"{'' if artist_num is None else artist_num}/"
        f"{'' if release_num is None else release_num}"
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
