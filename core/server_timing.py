"""Parse an upstream ``Server-Timing`` response header into ordered legs.

request-o-matic forwards library-metadata-lookup's per-stage timings by merging
LML's ``Server-Timing`` header into its own (see ``routers/request.py``). This
module holds the one stdlib-only helper that turns LML's header value into the
``(name, dur_ms)`` pairs rom feeds to
``wxyc_fastapi.observability.RequestTelemetry.as_server_timing(extra=...)``.

Kept deliberately dependency-free and defensive: a peer's header is untrusted
input on rom's response path, so a missing or malformed value degrades to an
empty result rather than raising. If a second consumer ever appears, this is the
natural thing to promote into wxyc-fastapi alongside ``as_server_timing``.
"""

from __future__ import annotations

import math
import re

__all__ = ["parse_server_timing"]

# A Server-Timing metric name is an RFC 7230 token (the same grammar HTTP header
# field-names use). Validating against it means a peer value with an interior
# space, comma, or — critically — a CR/LF never gets copied verbatim into rom's
# response header, where latin-1 would encode the CR/LF and fail at the ASGI
# send layer (outside the caller's try/except).
_TOKEN_RE = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+")


def parse_server_timing(header: str | None) -> list[tuple[str, float]]:
    """Parse a ``Server-Timing`` header value into ordered ``(name, dur_ms)`` pairs.

    Follows the `Server-Timing grammar
    <https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Server-Timing>`_
    loosely enough to survive real peer output: entries are comma-separated,
    each is ``name`` followed by optional ``;key=value`` params, and only the
    ``dur`` param is extracted (``desc`` and any others are ignored — rom has no
    consumer for them).

    Defensive by construction, because the value is a peer's header merged into
    rom's own response:

    * ``None`` / empty / whitespace-only -> ``[]``.
    * An entry whose name is not a valid RFC 7230 token (interior whitespace,
      comma, control chars) is skipped — it cannot be re-emitted into rom's own
      ``Server-Timing`` header without corrupting it.
    * An entry with no ``dur`` (a bare metric name) is skipped: it cannot merge
      into ``as_server_timing(extra=...)``, which requires a float.
    * An entry whose ``dur`` is non-numeric, non-finite (``inf`` / ``nan``), or
      negative is skipped without affecting the other entries — ``float()``
      accepts those spellings but they are not valid Server-Timing durations and
      would render as e.g. ``dur=inf`` downstream.

    Order and duplicate names are preserved (a list, not a dict) so the caller
    decides how to de-duplicate before merging.
    """
    if not header:
        return []

    legs: list[tuple[str, float]] = []
    for raw_entry in header.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue

        parts = entry.split(";")
        name = parts[0].strip()
        if not _TOKEN_RE.fullmatch(name):
            continue

        dur: float | None = None
        for param in parts[1:]:
            key, sep, value = param.partition("=")
            if sep and key.strip().lower() == "dur":
                try:
                    parsed = float(value.strip())
                except ValueError:
                    break
                if math.isfinite(parsed) and parsed >= 0:
                    dur = parsed
                break

        if dur is None:
            continue
        legs.append((name, dur))

    return legs
