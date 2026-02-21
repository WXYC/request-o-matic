"""Smoke tests for core.orchestration module.

Verifies that all 12 functions moved from routers.request are importable.
"""

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
