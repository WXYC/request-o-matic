"""Tests for core.orchestration module."""

from unittest.mock import AsyncMock

import pytest

from core.orchestration import (
    _resolve_fallback_artwork,
    build_context_message,
    fetch_artwork_for_items,
    filter_results_by_artist,
    filter_results_by_track_validation,
    limit_results,
    resolve_albums_for_track,
    search_album_fuzzy,
    search_compilations_for_track,
    search_library_with_fallback,
    search_song_as_artist,
    search_with_alternative_interpretation,
)
from library.models import LibraryItem
from tests.scenarios import FLOW_COMA_808_STATE


def test_all_functions_importable():
    """All 12 orchestration functions should be importable from core.orchestration."""
    functions = [
        limit_results,
        resolve_albums_for_track,
        filter_results_by_artist,
        search_with_alternative_interpretation,
        search_song_as_artist,
        search_library_with_fallback,
        search_compilations_for_track,
        search_album_fuzzy,
        filter_results_by_track_validation,
        _resolve_fallback_artwork,
        fetch_artwork_for_items,
        build_context_message,
    ]
    assert len(functions) == 12
    assert all(callable(f) for f in functions)


@pytest.mark.asyncio
async def test_search_album_fuzzy_rejects_subset_match():
    """search_album_fuzzy should reject a short library title that is a token subset of the query.

    Bug (FLOW_COMA_808_STATE): token_set_ratio scores ~100% when one string's tokens
    are a full subset of another, regardless of length difference. This causes short
    library titles to falsely match long Discogs album names.

    Example: "Essential Dance Music Classics Volume Three" would match library album
    "Dance Music" because {"dance", "music"} are both keywords (kw=2) and
    token_set_ratio=100%, but fuzz.ratio=41% correctly penalizes the length mismatch.
    """
    _ = FLOW_COMA_808_STATE  # link to scenario

    db = AsyncMock()
    # Exact search returns nothing, triggering fuzzy path
    # Fuzzy search returns the false positive
    db.search = AsyncMock(
        side_effect=[
            [],  # exact search: no results
            [
                LibraryItem(
                    id=1,
                    title="Dance Music",
                    artist="Various Artists",
                    genre="Electronic",
                    format="cd",
                    call_letters="EI",
                    artist_call_number=1,
                    release_call_number=1,
                )
            ],  # fuzzy search: returns false positive
        ]
    )

    results = await search_album_fuzzy(db, "Essential Dance Music Classics Volume Three")
    assert results == [], (
        "Should reject 'Dance Music' as a match for "
        "'Essential Dance Music Classics Volume Three' — "
        "token_set_ratio is 100% due to subset bias but fuzz.ratio is only 41%"
    )


@pytest.mark.asyncio
async def test_search_album_fuzzy_rejects_subset_match_on_exact_search():
    """search_album_fuzzy should also filter exact FTS5 results by length-sensitive similarity.

    Bug (FLOW_COMA_808_STATE): FTS5 tokenizes "The Best Of 808 State: Blueprint" and
    matches "808 State" because it contains tokens "808" and "State". The fuzz.ratio gate
    was only on the fuzzy fallback path, so the exact search returned false positives.
    """
    _ = FLOW_COMA_808_STATE  # link to scenario

    db = AsyncMock()
    # Exact FTS5 search returns the false positive directly
    db.search = AsyncMock(
        return_value=[
            LibraryItem(
                id=958,
                title="808 State",
                artist="808 State",
                genre="Electronic",
                format="vinyl",
                call_letters="Ei",
                artist_call_number=1,
                release_call_number=1,
            )
        ]
    )

    results = await search_album_fuzzy(db, "The Best Of 808 State: Blueprint")
    assert results == [], (
        "Should reject '808 State' as a match for "
        "'The Best Of 808 State: Blueprint' — "
        "FTS5 matches on shared tokens but fuzz.ratio correctly penalizes length mismatch"
    )
