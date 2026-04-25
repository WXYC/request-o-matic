"""Test factories for shared API contract models.

The generated ``LibraryCatalogItem`` and ``DiscogsMatchResult`` make several
fields required that used to be optional or computed on the local types.
These factories supply sensible WXYC-style defaults so unit tests don't have
to spell out the full constructor each time.
"""

from generated.api_models import DiscogsMatchResult, LibraryCatalogItem


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
    kwargs.setdefault("library_url", f"http://www.wxyc.info/wxycdb/libraryRelease?id={id}")
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
