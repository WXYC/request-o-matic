"""Search orchestration functions for the request pipeline.

These functions handle the multi-step search flow:
- Album resolution via Discogs
- Library search with fallback strategies
- Artist filtering and result limiting
- Compilation search and track validation
- Artwork fetching with fallback to artist/label images
- Context message construction for Slack
"""

from __future__ import annotations

import asyncio
import logging
import re

from core.matching import (
    MAX_SEARCH_RESULTS,
    deduplicate,
    extract_significant_words,
    is_compilation_artist,
    item_matches_artist,
    normalize_text,
    sort_by_title_relevance,
)
from discogs.lookup import lookup_releases_by_artist, lookup_releases_by_track
from discogs.models import DiscogsSearchRequest, DiscogsSearchResult
from discogs.service import DiscogsService
from library.db import LibraryDB
from library.models import LibraryItem
from services.parser import ParsedRequest

logger = logging.getLogger(__name__)


def limit_results(results: list) -> list:
    """Limit results to MAX_SEARCH_RESULTS.

    Args:
        results: List of search results

    Returns:
        First MAX_SEARCH_RESULTS items from the list
    """
    return results[:MAX_SEARCH_RESULTS]


async def resolve_albums_for_track(
    parsed: ParsedRequest,
    discogs_service: DiscogsService | None = None,
) -> tuple[list[str], bool]:
    """Resolve album names for a track if not provided.

    Searches Discogs for ALL releases containing the track, not just the first one.
    This ensures we find songs on both EPs and full albums (e.g., "Percolator" is on
    both "Noises [EP]" and "Emperor Tomato Ketchup" by Stereolab).

    Args:
        parsed: Parsed request with song/artist info
        discogs_service: Optional Discogs service with cache

    Returns:
        Tuple of (list of album names, song_not_found_flag)
    """
    # Check if album is missing or if album == artist (parser error)
    # When the parser can't identify the album, it sometimes uses the artist name
    album_is_missing = not parsed.album
    album_is_artist = (
        parsed.album
        and parsed.artist
        and parsed.album.lower().strip() == parsed.artist.lower().strip()
    )

    # Only do track lookup if we have an artist - without an artist, Discogs
    # results are unreliable (e.g., "Laid Back" could match any track with that name)
    if parsed.song and parsed.artist and (album_is_missing or album_is_artist):
        if album_is_artist:
            logger.info(f"Album '{parsed.album}' appears to be artist name, looking up albums")
        try:
            # Get ALL releases containing this track, not just the first one
            releases = await lookup_releases_by_track(
                parsed.song, parsed.artist, limit=10, service=discogs_service
            )
            if releases:
                # Extract unique album names from all validated releases.
                # No artist filter here — lookup_releases_by_track already validated
                # each release via validate_track_on_release, which handles aliases
                # (e.g., "Plug" -> "Luke Vibert").
                albums = []
                for _release_artist, album in releases:
                    if album not in albums:
                        albums.append(album)
                if albums:
                    logger.info(f"Found {len(albums)} albums for song '{parsed.song}': {albums}")
                    return albums, False
            logger.info(f"Could not find albums for song '{parsed.song}'")
            return [], True
        except Exception as e:
            logger.warning(f"Track lookup failed: {e}")
            return [], True
    return [parsed.album] if parsed.album else [], False


def filter_results_by_artist(
    results: list[LibraryItem],
    artist: str | None,
) -> list[LibraryItem]:
    """Filter library results to only include those matching the artist.

    Requires the searched artist name to appear at the START of the result's
    artist field (case-insensitive). This prevents false positives like
    "Toy" matching "Chew Toy". Compilations are NOT accepted here — this is
    strict artist filtering (unlike keyword search which allows compilations).

    Args:
        results: List of library items from search
        artist: Artist name to filter by

    Returns:
        Filtered list containing only items where artist matches
    """
    if not artist:
        return results

    filtered = [
        item for item in results if item_matches_artist(item, artist, allow_compilations=False)
    ]

    if len(filtered) < len(results):
        logger.info(
            f"Filtered {len(results)} results to {len(filtered)} matching artist '{artist}'"
        )

    return filtered


async def search_with_alternative_interpretation(
    db: LibraryDB,
    part1: str,
    part2: str,
) -> tuple[list[LibraryItem], None]:
    """Try searching with both artist/title interpretations.

    When given "X - Y" or "X. Y" format, tries:
    1. part1 as artist, part2 as title
    2. part2 as artist, part1 as title

    Args:
        db: Library database
        part1: First part of the ambiguous format
        part2: Second part of the ambiguous format

    Returns:
        Tuple of (results, None). Results from whichever interpretation finds matches.
    """
    # Try interpretation 1: part1 = artist
    query1 = f"{part1} {part2}"
    results1 = await db.search(query=query1, limit=MAX_SEARCH_RESULTS)
    results1 = filter_results_by_artist(results1, part1)

    # Try interpretation 2: part2 = artist
    query2 = f"{part2} {part1}"
    results2 = await db.search(query=query2, limit=MAX_SEARCH_RESULTS)
    results2 = filter_results_by_artist(results2, part2)

    # Return whichever has results (prefer the one with more/better matches)
    if results1 and not results2:
        logger.info(f"Alternative search matched with '{part1}' as artist")
        return results1, None
    elif results2 and not results1:
        logger.info(f"Alternative search matched with '{part2}' as artist")
        return results2, None
    elif results1 and results2:
        # Both have results - combine and dedupe by id
        logger.info("Alternative search matched both interpretations, combining results")
        return limit_results(deduplicate(results1 + results2)), None

    return [], None


async def search_song_as_artist(
    db: LibraryDB, song_as_artist: str, discogs_service: DiscogsService | None = None
) -> tuple[list[LibraryItem], None]:
    """Try searching using the parsed song title as an artist name.

    This handles cases where the AI parser misinterpreted an artist name
    as a song title (e.g., "Laid Back" parsed as song instead of artist).

    Strategy:
    1. Search library for direct artist match
    2. If no results, search Discogs for releases by that artist
    3. Cross-reference Discogs album titles with library (for compilations)

    Args:
        db: Library database
        song_as_artist: The song title to try as an artist name

    Returns:
        Tuple of (results, None). Results matching the artist, or empty list.
    """
    logger.info(f"Trying song '{song_as_artist}' as artist name")

    # Step 1: Direct library search for artist
    results = await db.search(query=song_as_artist, limit=MAX_SEARCH_RESULTS)
    results = filter_results_by_artist(results, song_as_artist)
    if results:
        logger.info(f"Found {len(results)} results treating '{song_as_artist}' as artist")
        return results, None

    # Step 2: Search Discogs for releases by this artist
    logger.info(f"No direct matches, searching Discogs for releases by '{song_as_artist}'")
    discogs_releases = await lookup_releases_by_artist(
        song_as_artist, limit=10, service=discogs_service
    )

    if not discogs_releases:
        logger.info(f"No Discogs releases found for '{song_as_artist}'")
        return [], None

    logger.info(f"Found {len(discogs_releases)} Discogs releases for '{song_as_artist}'")

    # Step 3: Cross-reference album titles with library
    seen_ids = set()
    for _discogs_artist, album_title in discogs_releases:
        if not album_title:
            continue

        # Search library for this album title
        album_results = await db.search(query=album_title, limit=MAX_SEARCH_RESULTS)

        for item in album_results:
            if item.id in seen_ids:
                continue

            # Accept if it's the actual artist or a compilation
            if item_matches_artist(item, song_as_artist):
                results.append(item)
                seen_ids.add(item.id)
                logger.info(f"Found '{item.artist} - {item.title}' via Discogs cross-reference")

        if len(results) >= MAX_SEARCH_RESULTS:
            break

    if results:
        logger.info(
            f"Found {len(results)} results via Discogs cross-reference for '{song_as_artist}'"
        )

    return limit_results(results), None


async def search_library_with_fallback(
    db: LibraryDB,
    parsed: ParsedRequest,
    albums: list[str],
) -> tuple[list[LibraryItem], bool]:
    """Search library with artist+album(s), falling back to artist+song or artist-only.

    Search order:
    1. Artist + each album (from Discogs lookup) - searches ALL albums, dedupes results
    2. Artist + song (song title might match album title)
    3. Artist only

    Note: Artist spelling correction should be done before calling this function.

    Args:
        db: Library database
        parsed: Parsed request (with corrected artist name)
        albums: List of resolved album names (may be empty)

    Returns:
        Tuple of (library_results, song_not_found_flag)
    """
    all_results: list[LibraryItem] = []
    seen_ids: set[int] = set()

    # Search for each album from Discogs
    if parsed.artist and albums:
        for album in albums:
            query = f"{parsed.artist} {album}"
            results = await db.search(query=query, limit=MAX_SEARCH_RESULTS)
            results = filter_results_by_artist(results, parsed.artist)

            # Filter to only include albums that match the Discogs album name
            # This prevents fuzzy search from returning unrelated albums by the same artist
            album_normalized = normalize_text(album)
            # Extract significant words from the Discogs album title (exclude stopwords)
            album_words = extract_significant_words(album, min_length=2)
            filtered_results = []
            for item in results:
                item_normalized = normalize_text(item.title or "")
                # Check if the library album title shares significant words with Discogs album
                item_words = extract_significant_words(item.title or "", min_length=2)
                # Require meaningful overlap to avoid false positives from common words
                # - Short titles (1-2 words): Discogs album must START with the library title
                #   (e.g., "Wireless" matches "Wireless - Live At...", but "The Band" doesn't
                #   match "Live Band # One" because it doesn't start with "The Band")
                # - Longer titles: require at least 2 common significant words
                common_words = album_words & item_words
                if len(item_words) <= 2:
                    # Short title: Discogs album must start with library title
                    if album_normalized.startswith(item_normalized):
                        filtered_results.append(item)
                elif len(common_words) >= 2:
                    filtered_results.append(item)
            results = filtered_results

            # Add unique results
            for item in results:
                if item.id not in seen_ids:
                    seen_ids.add(item.id)
                    all_results.append(item)

        if all_results:
            # Sort to prioritize results matching the first (primary) album
            sort_by_title_relevance(all_results, albums[0])
            return all_results, False

    # If no albums from Discogs, try artist + song
    # This is a fallback when we couldn't confirm which album contains the track
    if parsed.artist and parsed.song:
        query = f"{parsed.artist} {parsed.song}"
        results = await db.search(query=query, limit=MAX_SEARCH_RESULTS)
        results = filter_results_by_artist(results, parsed.artist)

        if results:
            # Prioritize results where album title matches song title
            sort_by_title_relevance(results, parsed.song)
            # We had a song but couldn't find/confirm albums from Discogs
            # Set song_not_found=True so context message indicates uncertainty
            return results, True

    # If still no results, try just artist
    if not all_results and parsed.artist:
        logger.info(f"No results for albums {albums}, trying artist only: '{parsed.artist}'")
        results = await db.search(query=parsed.artist, limit=MAX_SEARCH_RESULTS)
        results = filter_results_by_artist(results, parsed.artist)
        if results:
            return results, True

    return all_results, False


async def _keyword_search_for_track(
    db: LibraryDB,
    parsed: ParsedRequest,
) -> list[LibraryItem]:
    """Search the library for a track using keyword matching.

    Builds a query from significant words in the artist and song title,
    then filters results to the requested artist or compilation albums.

    Args:
        db: Library database
        parsed: Parsed request with song/artist info (both must be non-None)

    Returns:
        List of matching library items (may be empty)
    """
    try:
        sig_artist = (
            list(extract_significant_words(parsed.artist, min_length=3)) if parsed.artist else []
        )
        sig_song = list(extract_significant_words(parsed.song, min_length=3)) if parsed.song else []

        # Include both artist words (max 2) and song words (max 2) to find the right album
        query_words = sig_artist[:2] + sig_song[:2]

        if not query_words:
            return []

        keyword_query = " ".join(query_words)
        logger.info(f"Trying direct keyword search: '{keyword_query}'")
        keyword_results = await db.search(query=keyword_query, limit=MAX_SEARCH_RESULTS)

        if not keyword_results:
            return []

        # Filter by artist unless it's a compilation album
        assert parsed.artist is not None  # guaranteed by caller
        filtered_results = [
            item for item in keyword_results if item_matches_artist(item, parsed.artist)
        ]

        if filtered_results:
            logger.info(
                f"Found {len(filtered_results)} matches via keyword search (after artist filter)"
            )

        return filtered_results
    except Exception as e:
        logger.warning(f"Keyword search failed: {e}")
        return []


async def _discogs_cross_reference(
    db: LibraryDB,
    parsed: ParsedRequest,
    discogs_service: DiscogsService | None = None,
) -> tuple[list[LibraryItem], dict[int, str]]:
    """Cross-reference Discogs track listings with the library.

    Searches Discogs for releases containing the track, then looks up each
    release in the library with fuzzy matching. Filters results to albums by
    the requested artist or compilation/soundtrack albums.

    Args:
        db: Library database
        parsed: Parsed request with song/artist info (both must be non-None)
        discogs_service: Optional Discogs service with cache

    Returns:
        Tuple of (matching library items, dict mapping item_id to Discogs album title)
    """
    results: list[LibraryItem] = []
    seen_ids: set[int] = set()
    discogs_titles: dict[int, str] = {}

    assert parsed.song is not None and parsed.artist is not None  # guaranteed by caller

    try:
        # Extract full song name with remix/version info
        raw_lower = parsed.raw_message.lower()
        song_search: str = parsed.song

        remix_match = re.search(r"\((.*?(?:remix|mix|version|edit).*?)\)", raw_lower, re.IGNORECASE)
        if remix_match and parsed.song.lower() in raw_lower:
            song_search = f"{parsed.song} ({remix_match.group(1)})"
            logger.info(f"Using full track name with version info: '{song_search}'")

        releases = await lookup_releases_by_track(
            song_search, parsed.artist, service=discogs_service
        )
        logger.info(f"Found {len(releases)} releases with '{song_search}' on Discogs")

        # Check each release against our library
        for release_artist, release_album in releases:
            # Skip if the "album" is just the artist name (Discogs artifact)
            if parsed.artist and release_album.lower().strip() == parsed.artist.lower().strip():
                logger.debug(f"Skipping '{release_album}' - appears to be artist name, not album")
                continue

            # Skip very short album titles (likely artifacts)
            if len(release_album.strip()) < 3:
                continue

            matches = await search_album_fuzzy(db, release_album)

            # Filter matches to only include albums by the requested artist OR compilations
            # This prevents "Mad Love" by Lush matching "Love is Gone Mad" by Big Eyes
            # but still allows Various Artists compilations and soundtracks through
            if matches and parsed.artist:
                # Only allow compilation matches when Discogs says the release
                # is by a compilation artist
                discogs_is_compilation = is_compilation_artist(release_artist)
                matches = [
                    match
                    for match in matches
                    if item_matches_artist(
                        match,
                        parsed.artist,
                        allow_compilations=discogs_is_compilation,
                    )
                ]

            if matches:
                logger.info(
                    f"Found '{parsed.song}' in library on '{matches[0].title}' "
                    f"(matched from Discogs: '{release_album}')"
                )
                # Add matches, deduplicating by ID
                for match in matches:
                    if match.id not in seen_ids:
                        results.append(match)
                        seen_ids.add(match.id)
                        # Store the Discogs album title for artwork lookup
                        discogs_titles[match.id] = release_album

                if len(results) >= MAX_SEARCH_RESULTS:
                    break
    except Exception as e:
        logger.warning(f"Failed to search for track on other releases: {e}")

    return results, discogs_titles


async def search_compilations_for_track(
    db: LibraryDB,
    parsed: ParsedRequest,
    discogs_service: DiscogsService | None = None,
) -> tuple[list[LibraryItem], dict[int, str]]:
    """Search for track on compilation albums using Discogs and library keyword search.

    Orchestrates two search strategies:
    1. Keyword search in the library (fast, but may return false positives)
    2. Discogs cross-reference (slower, but knows actual track listings)

    Discogs results are preferred; keyword matches are used as fallback.

    Args:
        db: Library database
        parsed: Parsed request with song/artist info
        discogs_service: Optional Discogs service with cache

    Returns:
        Tuple of (list of matching library items, dict mapping item_id to discogs_album_title)
    """
    if not parsed.song or not parsed.artist:
        return [], {}

    logger.info(f"Searching for '{parsed.song}' on other releases (compilations, etc.)")

    # Strategy 1: Direct library keyword search (kept aside as fallback)
    keyword_matches = await _keyword_search_for_track(db, parsed)

    # Strategy 2: Discogs cross-reference (preferred -- knows actual track listings)
    results, discogs_titles = await _discogs_cross_reference(db, parsed, discogs_service)

    # If Discogs didn't find anything, fall back to keyword matches
    if not results and keyword_matches:
        logger.info("Discogs search found nothing, using keyword matches as fallback")
        results = deduplicate(results + keyword_matches[:1])

    # Prioritize albums whose title matches the song title
    # (e.g., "Meet Me in the City" album for song "Meet Me in the City")
    if results and parsed.song:
        sort_by_title_relevance(results, parsed.song)

    return limit_results(results), discogs_titles


async def search_album_fuzzy(db: LibraryDB, album_title: str) -> list[LibraryItem]:
    """Search for album with fuzzy keyword matching.

    Args:
        db: Library database
        album_title: Album title to search for

    Returns:
        List of matching library items
    """
    from rapidfuzz import fuzz

    # Try exact search first (use higher limit to allow artist filtering later)
    results = await db.search(query=album_title, limit=MAX_SEARCH_RESULTS)

    if not results:
        significant_words = list(extract_significant_words(album_title, min_length=3))

        if significant_words:
            fuzzy_query = " ".join(significant_words[:4])
            logger.info(f"Exact match failed for '{album_title}', trying fuzzy: '{fuzzy_query}'")
            results = await db.search(query=fuzzy_query, limit=MAX_SEARCH_RESULTS)

            # Filter results using both keyword matching AND overall similarity
            if results:
                album_lower = album_title.lower()
                filtered_results = []
                for result in results:
                    result_title_lower = (result.title or "").lower()

                    # Count keyword matches
                    keyword_matches = sum(
                        1 for word in significant_words if word in result_title_lower
                    )

                    # Calculate overall fuzzy similarity
                    similarity = fuzz.token_set_ratio(album_lower, result_title_lower)

                    # Require BOTH: 2+ keyword matches AND 60% overall similarity
                    # This prevents "22 Explosive Hits, Vol 2" matching "K-Tel: 22 Explosive Hits!"
                    # which share keywords but are different albums (similarity ~50%)
                    if keyword_matches >= 2 and similarity >= 60:
                        logger.debug(
                            f"Album match: '{result.title}' "
                            f"(keywords={keyword_matches}, similarity={similarity})"
                        )
                        filtered_results.append(result)
                    else:
                        logger.debug(
                            f"Album rejected: '{result.title}' "
                            f"(keywords={keyword_matches}, similarity={similarity})"
                        )

                results = filtered_results

    return results


async def filter_results_by_track_validation(
    results: list[LibraryItem],
    song: str | None,
    artist: str | None,
    discogs_service: DiscogsService | None,
) -> list[LibraryItem] | None:
    """Filter fallback results to only albums that contain the requested track.

    When the search pipeline falls back to returning all artist albums
    (song_not_found=True), this function validates each album against Discogs
    to determine which ones actually contain the requested track.

    Args:
        results: Fallback library results (all albums by artist)
        song: Requested song title
        artist: Requested artist name
        discogs_service: Discogs service for API lookups

    Returns:
        Filtered list of results containing the track, or None if validation
        isn't possible (no Discogs service, no song/artist, or no albums validated)
    """
    if not discogs_service or not song or not artist or not results:
        return None

    async def validate_one(item: LibraryItem) -> LibraryItem | None:
        try:
            response = await discogs_service.search(
                DiscogsSearchRequest(album=item.title, artist=artist)
            )
            if not response.results:
                return None

            best_result = response.results[0]
            if best_result.release_id:
                is_valid = await discogs_service.validate_track_on_release(
                    best_result.release_id, song, artist
                )
                if is_valid:
                    logger.info(
                        f"Track validation: '{song}' confirmed on '{item.title}' "
                        f"(release {best_result.release_id})"
                    )
                    return item
        except Exception as e:
            logger.warning(f"Track validation failed for '{item.title}': {e}")
        return None

    validation_results = await asyncio.gather(*[validate_one(item) for item in results])
    validated = [r for r in validation_results if r is not None]

    if validated:
        logger.info(
            f"Track validation filtered {len(results)} albums to {len(validated)} "
            f"containing '{song}'"
        )
        return validated

    logger.info(f"Track validation could not confirm '{song}' on any album")
    return None


async def _resolve_fallback_artwork(discogs_service: DiscogsService, release_id: int) -> str | None:
    """Try artist image, then label image, for a release with no cover art."""
    release = await discogs_service.get_release(release_id)
    if not release:
        return None

    if release.artist_id:
        image = await discogs_service.get_artist_image(release.artist_id)
        if image:
            logger.info(f"Using artist image fallback for release {release_id}")
            return image

    if release.label_id:
        image = await discogs_service.get_label_image(release.label_id)
        if image:
            logger.info(f"Using label image fallback for release {release_id}")
            return image

    return None


async def fetch_artwork_for_items(
    items: list[LibraryItem],
    discogs_service: DiscogsService | None,
    discogs_titles: dict[int, str] | None = None,
) -> list[tuple[LibraryItem, DiscogsSearchResult | None]]:
    """Fetch artwork for multiple library items in parallel.

    Args:
        items: List of library items
        discogs_service: Discogs service instance
        discogs_titles: Optional dict mapping item_id to Discogs album title

    Returns:
        List of (item, artwork) tuples
    """
    if not discogs_service:
        return [(item, None) for item in items]

    discogs_titles = discogs_titles or {}

    async def fetch_one(item: LibraryItem) -> DiscogsSearchResult | None:
        try:
            # Use Discogs album title if we have it (from compilation search)
            album = discogs_titles.get(item.id, item.title)

            # For compilations, simplify artist to "Various" for Discogs lookup
            # Library formats like "Various Artists - Rock - C" or "Soundtracks - M" won't match Discogs
            artist = item.artist or ""
            if is_compilation_artist(artist):
                artist = "Various"

            response = await discogs_service.search(
                DiscogsSearchRequest(album=album, artist=artist)
            )
            # Return best result (already sorted by confidence)
            if response.results:
                result = response.results[0]
                if not result.artwork_url:
                    fallback = await _resolve_fallback_artwork(discogs_service, result.release_id)
                    if fallback:
                        result = result.model_copy(update={"artwork_url": fallback})
                return result
            return None
        except Exception as e:
            logger.warning(f"Artwork lookup failed for {item.title}: {e}")
            return None

    artwork_results = await asyncio.gather(*[fetch_one(item) for item in items])
    return list(zip(items, artwork_results, strict=True))


def build_context_message(
    parsed: ParsedRequest,
    found_on_compilation: bool,
    song_not_found: bool,
    has_results: bool = True,
) -> str | None:
    """Build context message for Slack based on search results.

    Args:
        parsed: Parsed request
        found_on_compilation: Whether song was found on compilation
        song_not_found: Whether the exact song/album wasn't found
        has_results: Whether there are library results to show

    Returns:
        Context message string or None
    """
    if found_on_compilation:
        return f'Found "{parsed.song}" by {parsed.artist} on:'

    if song_not_found and has_results:
        # Show "here are other albums" only if we have results to show
        if parsed.song and parsed.album:
            return f'"{parsed.album}" not found in the library, but here are other albums by {parsed.artist}:'
        elif parsed.song:
            return f'"{parsed.song}" is not on any album in the library, but here are some albums by {parsed.artist}:'
    elif song_not_found and not has_results:
        # No results at all after filtering
        if parsed.song and parsed.artist:
            return f'"{parsed.song}" by {parsed.artist} not found in library.'

    return None
