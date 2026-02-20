"""Shared matching constants and utilities for search operations.

This module centralizes the matching rules used throughout the search flow.
Constants were consolidated from multiple locations to ensure consistency.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Hashable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from discogs.models import TrackItem

# =============================================================================
# Search Result Limiting
# =============================================================================

MAX_SEARCH_RESULTS = 5
"""Maximum number of results to return from search operations."""


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


def extract_significant_words(text: str, min_length: int = 3) -> set[str]:
    """Extract significant words from text after normalization.

    Applies the full pipeline: lowercase, remove punctuation, split into words,
    filter by minimum length, and remove stopwords.

    Args:
        text: Input text to extract words from
        min_length: Exclude words with length <= this value (e.g., min_length=3
            excludes words of 3 or fewer characters)

    Returns:
        Set of significant lowercase words
    """
    normalized = normalize_text(text)
    return {w for w in normalized.split() if len(w) > min_length and w not in STOPWORDS}


def matches_artist_or_compilation(item_artist: str, target_artist: str) -> bool:
    """Check if an item's artist matches the target via prefix, or is a compilation.

    This combines the common pattern of checking whether a library item's artist
    field starts with the searched artist name (prefix matching to avoid false
    positives like "Toy" matching "Chew Toy") OR is a compilation/soundtrack.

    Args:
        item_artist: Artist name from a library item or search result
        target_artist: Artist name being searched for

    Returns:
        True if item_artist starts with target_artist or is a compilation artist
    """
    item_lower = item_artist.lower()
    target_lower = target_artist.lower()
    return item_lower.startswith(target_lower) or is_compilation_artist(item_lower)


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
# Format Detection
# =============================================================================


def detect_ambiguous_format(raw_message: str) -> tuple[str, str] | None:
    """Detect if message has ambiguous 'X - Y' or 'X. Y' format.

    These formats are ambiguous because they could be interpreted as either:
    - Artist: X, Title: Y
    - Title: X, Artist: Y

    Args:
        raw_message: The original request message

    Returns:
        Tuple of (part1, part2) if ambiguous format detected, None otherwise.
    """
    # Check for "X - Y" pattern with various spacing around dash
    # Matches: "X - Y", "X- Y", "X -Y" (requires at least one space to avoid "hip-hop")
    dash_match = re.search(r"(.+?)\s*-\s+(.+)|(.+?)\s+-\s*(.+)", raw_message)
    if dash_match:
        # Groups 1,2 for "X- Y" pattern, groups 3,4 for "X -Y" pattern
        if dash_match.group(1) and dash_match.group(2):
            part1, part2 = dash_match.group(1).strip(), dash_match.group(2).strip()
        else:
            part1, part2 = dash_match.group(3).strip(), dash_match.group(4).strip()
        if part1 and part2:
            return (part1, part2)

    # Check for "X. Y" pattern (period followed by space)
    if ". " in raw_message:
        parts = raw_message.split(". ", 1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return (parts[0].strip(), parts[1].strip())

    return None


# =============================================================================
# Deduplication
# =============================================================================


def deduplicate[T](
    items: list[T],
    key: Callable[[T], Hashable] = lambda x: x.id,  # type: ignore[attr-defined]
) -> list[T]:
    """Remove duplicates from a list, preserving first-occurrence order.

    Args:
        items: List of items to deduplicate
        key: Function to extract a hashable key from each item (default: item.id)

    Returns:
        New list with duplicates removed
    """
    seen: set[Hashable] = set()
    result: list[T] = []
    for item in items:
        k = key(item)
        if k not in seen:
            seen.add(k)
            result.append(item)
    return result


# =============================================================================
# Sort by Title Relevance
# =============================================================================


def sort_by_title_relevance(items: list, target: str) -> None:
    """Sort items in-place so items whose title contains the target come first.

    Args:
        items: List of items with a ``title`` attribute (may be None)
        target: Target string to match against item titles (case-insensitive)
    """
    target_lower = target.lower()
    items.sort(
        key=lambda r: target_lower in (r.title or "").lower(),
        reverse=True,
    )


# =============================================================================
# Track Validation
# =============================================================================


def validate_track_on_tracklist(
    tracklist: list[TrackItem],
    release_artist: str,
    track: str,
    artist: str,
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

    return False
