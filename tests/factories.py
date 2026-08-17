"""Test factories for shared API contract models.

The generated ``LibraryCatalogItem`` and ``DiscogsMatchResult`` make several
fields required that used to be optional or computed on the local types.
These factories supply sensible WXYC-style defaults so unit tests don't have
to spell out the full constructor each time.
"""

from typing import Any

from generated.api_models import DiscogsMatchResult, LibraryCatalogItem
from routers.request import DEGRADED_PARSING, UnifiedResponse


def _compute_call_number(
    genre: str | None,
    format_: str | None,
    call_letters: str | None,
    artist_call_number: int | None,
    release_call_number: int | None,
) -> str:
    """Mirror LML's server-side call-number computation."""
    parts: list[str] = []
    if genre:
        parts.append(genre)
    if format_:
        parts.append(format_)
    if call_letters:
        parts.append(call_letters)
    if artist_call_number is not None:
        parts.append(str(artist_call_number))
    if release_call_number is not None and parts:
        parts[-1] = f"{parts[-1]}/{release_call_number}"
    return " ".join(parts)


def make_library_item(
    *,
    id: int = 1,
    artist: str | None = "Stereolab",
    title: str | None = "Aluminum Tunes",
    call_letters: str | None = "S",
    artist_call_number: int | None = 1,
    release_call_number: int | None = 1,
    genre: str | None = "Rock",
    format: str | None = "CD",
    **kwargs,
) -> LibraryCatalogItem:
    """Build a LibraryCatalogItem with computed call_number/library_url."""
    kwargs.setdefault(
        "call_number",
        _compute_call_number(genre, format, call_letters, artist_call_number, release_call_number),
    )
    kwargs.setdefault("library_url", f"https://dj.wxyc.org/dashboard/album/legacy/{id}")
    return LibraryCatalogItem(
        id=id,
        artist=artist,
        title=title,
        call_letters=call_letters,
        artist_call_number=artist_call_number,
        release_call_number=release_call_number,
        genre=genre,
        format=format,
        **kwargs,
    )


def make_release_metadata(
    *,
    release_id: int = 123,
    artist: str | None = "Stereolab",
    album: str | None = "Aluminum Tunes",
    **kwargs,
) -> DiscogsMatchResult:
    """Build a DiscogsMatchResult with a default release_url."""
    kwargs.setdefault("release_url", f"https://www.discogs.com/release/{release_id}")
    return DiscogsMatchResult(release_id=release_id, artist=artist, album=album, **kwargs)


def make_degraded_response(mode: str = DEGRADED_PARSING, **kwargs) -> dict[str, Any]:
    """Build the JSON body the service returns in a degraded mode.

    Derived from ``UnifiedResponse`` rather than hand-written, so a field added
    to the model shows up here automatically. Hand-rolled copies of this payload
    had already drifted -- they omitted ``degraded_mode`` while their comments
    claimed to be the exact production shape.

    Args:
        mode: The ``degraded_mode`` value; ``parsing_unavailable`` sends
            ``parsed=None``, matching what routers/request.py actually returns.
        **kwargs: Overrides applied to the UnifiedResponse constructor.

    Returns:
        A JSON-serializable dict, as a CLI would receive from ``/request``.
    """
    if mode == DEGRADED_PARSING:
        kwargs.setdefault("parsed", None)
    else:
        # Imported here, not at module scope: tests/conftest.py imports this
        # module, so pulling the parser in at import time would be a cycle.
        from services.parser import MessageType, ParsedRequest

        kwargs.setdefault(
            "parsed",
            ParsedRequest(
                song="la paradoja",
                artist="Juana Molina",
                is_request=True,
                message_type=MessageType.REQUEST,
                raw_message="play la paradoja by juana molina",
            ),
        )
    kwargs.setdefault("cache_stats", {})
    response = UnifiedResponse(degraded_mode=mode, **kwargs)
    return response.model_dump(mode="json")
