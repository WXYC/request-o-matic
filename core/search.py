"""Search strategy pattern for request handling.

This module provides a declarative way to define and execute search strategies.
Each strategy has explicit trigger conditions and can be easily tested in isolation.

Strategies are executed in array order until results are found.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from core.matching import detect_ambiguous_format
from library.db import LibraryDB
from library.models import LibraryItem
from services.parser import ParsedRequest


class SearchStrategyType(str, Enum):
    """Descriptive names for each search strategy.

    These names are used in telemetry to track which strategy succeeded.
    """

    ARTIST_PLUS_ALBUM = "artist_plus_album"
    """Search by artist + album/song name."""

    ARTIST_ONLY = "artist_only"
    """Fallback to just artist name when album/song search fails."""

    SWAPPED_INTERPRETATION = "swapped_interpretation"
    """Try "X - Y" format as both artist/title orderings."""

    TRACK_ON_COMPILATION = "track_on_compilation"
    """Find song on compilation albums via Discogs cross-reference."""

    KEYWORD_MATCH = "keyword_match"
    """Significant word extraction search."""


@dataclass
class SearchState:
    """Tracks state across strategy execution.

    This state is passed to each strategy's condition function to allow
    strategies to make decisions based on previous results.
    """

    results: list[LibraryItem] = field(default_factory=list)
    """Current search results."""

    song_not_found: bool = False
    """True if the exact song/album wasn't found (fell back to artist-only)."""

    found_on_compilation: bool = False
    """True if the song was found on a compilation album."""

    strategies_tried: list[SearchStrategyType] = field(default_factory=list)
    """List of strategies that have been executed."""

    discogs_titles: dict[int, str] = field(default_factory=dict)
    """Map of library item ID to Discogs album title (for artwork lookup)."""

    album_for_search: Optional[str] = None
    """Album name resolved from Discogs track lookup."""


# Type aliases for strategy functions
ConditionFunc = Callable[[ParsedRequest, SearchState, str], bool]
"""Function that returns True if a strategy should be executed.

Args:
    parsed: The parsed request
    state: Current search state
    raw_message: Original request message
"""

ExecuteFunc = Callable[..., Awaitable[tuple[list[LibraryItem], Any]]]
"""Async function that executes the search strategy.

Returns:
    Tuple of (results, metadata). Metadata varies by strategy:
    - ARTIST_PLUS_ALBUM: bool (fallback_used)
    - SWAPPED_INTERPRETATION: None
    - TRACK_ON_COMPILATION: dict (discogs_titles)
"""


@dataclass
class SearchStrategy:
    """Declarative search strategy with explicit trigger condition.

    Strategies are executed in priority order (array position).
    The first strategy that produces results wins.
    """

    name: SearchStrategyType
    """Strategy identifier for telemetry."""

    condition: ConditionFunc
    """Function that returns True if this strategy should run."""

    execute: ExecuteFunc
    """Async function that performs the search."""

    updates_song_not_found: bool = False
    """If True, the strategy's metadata (second return value) updates song_not_found."""

    updates_discogs_titles: bool = False
    """If True, the strategy's metadata contains discogs_titles to merge."""


# =============================================================================
# Strategy Conditions
# =============================================================================


def has_artist_and_album_or_song(
    parsed: ParsedRequest, state: SearchState, raw_message: str
) -> bool:
    """Condition: Has artist AND (album or song)."""
    return bool(parsed.artist and (state.album_for_search or parsed.song))


def no_results_and_ambiguous_format(
    parsed: ParsedRequest, state: SearchState, raw_message: str
) -> bool:
    """Condition: No results yet AND message has ambiguous X - Y format."""
    if state.results:
        return False
    return detect_ambiguous_format(raw_message) is not None


def song_not_found_with_artist_and_song(
    parsed: ParsedRequest, state: SearchState, raw_message: str
) -> bool:
    """Condition: Song not found AND we have both artist and song."""
    return state.song_not_found and bool(parsed.artist) and bool(parsed.song)


# =============================================================================
# Strategy Registry
# =============================================================================


def build_strategies(
    search_library_func: ExecuteFunc,
    search_alternative_func: ExecuteFunc,
    search_compilations_func: ExecuteFunc,
) -> list[SearchStrategy]:
    """Build the list of search strategies with injected execute functions.

    This allows the router to inject its own implementations while keeping
    the strategy pattern logic separate.

    Args:
        search_library_func: Function implementing ARTIST_PLUS_ALBUM search
        search_alternative_func: Function implementing SWAPPED_INTERPRETATION search
        search_compilations_func: Function implementing TRACK_ON_COMPILATION search

    Returns:
        List of SearchStrategy objects in execution order
    """
    return [
        SearchStrategy(
            name=SearchStrategyType.ARTIST_PLUS_ALBUM,
            condition=has_artist_and_album_or_song,
            execute=search_library_func,
            updates_song_not_found=True,
        ),
        SearchStrategy(
            name=SearchStrategyType.SWAPPED_INTERPRETATION,
            condition=no_results_and_ambiguous_format,
            execute=search_alternative_func,
        ),
        SearchStrategy(
            name=SearchStrategyType.TRACK_ON_COMPILATION,
            condition=song_not_found_with_artist_and_song,
            execute=search_compilations_func,
            updates_discogs_titles=True,
        ),
    ]


async def execute_search_pipeline(
    parsed: ParsedRequest,
    db: LibraryDB,
    raw_message: str,
    strategies: list[SearchStrategy],
    album_for_search: Optional[str] = None,
) -> SearchState:
    """Execute strategies in array order until results found.

    Args:
        parsed: The parsed request with artist/song/album
        db: Library database for searches
        raw_message: Original request message (for ambiguous format detection)
        strategies: List of search strategies to try
        album_for_search: Optional album name from Discogs lookup

    Returns:
        SearchState with results and metadata about the search
    """
    state = SearchState(
        results=[],
        strategies_tried=[],
        album_for_search=album_for_search,
    )

    for strategy in strategies:
        # Check if strategy should run
        if not strategy.condition(parsed, state, raw_message):
            continue

        state.strategies_tried.append(strategy.name)

        # Execute the strategy
        if strategy.name == SearchStrategyType.ARTIST_PLUS_ALBUM:
            results, fallback_used = await strategy.execute(db, parsed, state.album_for_search)
            if results:
                state.results = results
            if strategy.updates_song_not_found and fallback_used:
                state.song_not_found = True

        elif strategy.name == SearchStrategyType.SWAPPED_INTERPRETATION:
            # Parse the ambiguous format
            if " - " in raw_message:
                part1, part2 = raw_message.split(" - ", 1)
            else:
                part1, part2 = raw_message.split(". ", 1)
            part1 = part1.strip()
            part2 = part2.strip()

            results = await strategy.execute(db, part1, part2)
            if results:
                state.results = results
                state.song_not_found = False

        elif strategy.name == SearchStrategyType.TRACK_ON_COMPILATION:
            results, discogs_titles = await strategy.execute(db, parsed)
            if results:
                state.results = results
                state.found_on_compilation = True
                state.song_not_found = False
                if strategy.updates_discogs_titles:
                    state.discogs_titles = discogs_titles

        # Stop if we found results (unless we're doing compilation search which can replace results)
        if state.results and strategy.name != SearchStrategyType.TRACK_ON_COMPILATION:
            # For compilation search, we continue even if we have artist-only results
            # because finding the actual song is better than just artist albums
            if not state.song_not_found:
                break

    return state


def get_search_type_from_state(state: SearchState) -> str:
    """Derive the search type string for telemetry from state.

    Args:
        state: The completed search state

    Returns:
        String describing which search type succeeded
    """
    if state.found_on_compilation:
        return "compilation"

    if not state.strategies_tried:
        return "none"

    last_strategy = state.strategies_tried[-1]

    if last_strategy == SearchStrategyType.ARTIST_PLUS_ALBUM:
        return "fallback" if state.song_not_found else "direct"
    elif last_strategy == SearchStrategyType.SWAPPED_INTERPRETATION:
        return "alternative"
    elif last_strategy == SearchStrategyType.TRACK_ON_COMPILATION:
        return "compilation"

    return "none"
