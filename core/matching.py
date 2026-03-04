"""Shared matching constants and utilities for search operations.

This module centralizes the matching rules used by the library database,
Discogs service, and Discogs cache service.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from discogs.models import TrackItem


# =============================================================================
# Stopwords
# =============================================================================

STOPWORDS = frozenset(
    {
        # Articles
        "the",
        "a",
        "an",
        # Conjunctions/prepositions
        "and",
        "with",
        "from",
        # Demonstratives
        "that",
        "this",
        # Request-specific noise
        "play",
        "song",
        "remix",
        # Label/format noise
        "story",
        "records",
    }
)
"""Words to exclude when extracting significant keywords from search queries."""


# =============================================================================
# Compilation Detection
# =============================================================================

COMPILATION_KEYWORDS = frozenset(
    {
        "various",
        "soundtrack",
        "compilation",
        "v/a",
        "v.a.",
    }
)
"""Keywords indicating a compilation/soundtrack album (case-insensitive substring match)."""


def normalize_text(text: str) -> str:
    """Remove punctuation, normalize whitespace, and lowercase.

    Replaces all non-word, non-space characters with spaces, then collapses
    multiple spaces into one. Uses ASCII mode so only Latin letters, digits,
    and underscores are considered word characters (non-Latin scripts are
    stripped, matching the original ``[^a-z0-9\\s]`` behavior).

    Args:
        text: Input text to normalize

    Returns:
        Lowercased text with punctuation removed and whitespace normalized
    """
    result = re.sub(r"[^\w\s]", " ", text.lower(), flags=re.ASCII)
    return " ".join(result.split())


def is_compilation_artist(artist: str) -> bool:
    """Check if an artist name indicates a compilation/soundtrack album.

    Args:
        artist: Artist name to check

    Returns:
        True if artist contains compilation keywords (various, soundtrack, etc.)
    """
    if not artist:
        return False
    artist_lower = artist.lower()
    return any(keyword in artist_lower for keyword in COMPILATION_KEYWORDS)


# =============================================================================
# Confidence Scoring
# =============================================================================


def calculate_confidence(
    request_artist: str | None,
    request_album: str | None,
    result_artist: str,
    result_album: str,
) -> float:
    """Calculate confidence score for how well a search result matches a request.

    Scoring rules:
    - Exact artist match: +0.4
    - Partial artist match (substring): +0.3
    - Exact album match: +0.4
    - Partial album match (substring): +0.3
    - Both fields match well (score >= 0.6): +0.2 bonus
    - Minimum score for any result: 0.2

    Args:
        request_artist: Artist from the search request
        request_album: Album from the search request
        result_artist: Artist from the search result
        result_album: Album from the search result

    Returns:
        Confidence score between 0.2 and 1.0
    """
    score = 0.0

    def normalize(s: str | None) -> str:
        return s.lower().strip() if s else ""

    req_artist = normalize(request_artist)
    req_album = normalize(request_album)
    res_artist = normalize(result_artist)
    res_album = normalize(result_album)

    # Artist match
    if req_artist and res_artist:
        if req_artist == res_artist:
            score += 0.4
        elif req_artist in res_artist or res_artist in req_artist:
            score += 0.3

    # Album match
    if req_album and res_album:
        if req_album == res_album:
            score += 0.4
        elif req_album in res_album or res_album in req_album:
            score += 0.3

    # Bonus for both matches
    if score >= 0.6:
        score += 0.2

    # Base score if we got any result
    if score == 0:
        score = 0.2

    return min(score, 1.0)


# =============================================================================
# Track Validation
# =============================================================================


def validate_track_on_tracklist(
    tracklist: list[TrackItem],
    release_artist: str,
    track: str,
    artist: str,
    trust_release_artist: bool = False,
) -> bool:
    """Check whether a track by an artist appears on a tracklist.

    Handles both single-artist releases (checks release_artist) and
    compilations (checks per-track artists). Strips Discogs numbering
    like ``(2)`` from artist names before comparing.

    Args:
        tracklist: List of TrackItem objects from a release
        release_artist: Primary artist on the release
        track: Track title to find
        artist: Artist name to match
        trust_release_artist: If True and the track title matches on a
            single-artist release, accept the match even if the release artist
            doesn't match the searched artist. This handles Discogs aliases
            (e.g., "Plug" is an alias for "Luke Vibert").

    Returns:
        True if the track by the artist is found on the tracklist
    """
    track_lower = track.lower()
    artist_lower = artist.lower()

    for item in tracklist:
        item_title = item.title.lower()
        # Check if track title matches (substring in either direction)
        if track_lower not in item_title and item_title not in track_lower:
            continue

        # Check per-track artists first (for compilations)
        if item.artists:
            for track_artist in item.artists:
                track_artist_lower = track_artist.lower().split("(")[0].strip()
                if artist_lower in track_artist_lower or track_artist_lower in artist_lower:
                    return True
        else:
            # For single-artist releases, check release artist
            rel_artist = release_artist.lower()
            # Remove Discogs numbering like "(2)"
            rel_artist = rel_artist.split("(")[0].strip()

            if artist_lower in rel_artist or rel_artist in artist_lower:
                return True
            if trust_release_artist:
                return True

    return False
