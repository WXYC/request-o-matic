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

__all__ = ["parse_server_timing"]


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
    * An entry with no ``dur`` (a bare metric name) is skipped: it cannot merge
      into ``as_server_timing(extra=...)``, which requires a float.
    * An entry with a non-numeric ``dur`` is skipped without affecting the
      other entries.

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
        if not name:
            continue

        dur: float | None = None
        for param in parts[1:]:
            key, sep, value = param.partition("=")
            if sep and key.strip().lower() == "dur":
                try:
                    dur = float(value.strip())
                except ValueError:
                    dur = None
                break

        if dur is None:
            continue
        legs.append((name, dur))

    return legs
