"""Request handling router with refactored helper functions.

Search Strategy Decision Tree
=============================

The search flow follows these steps in order:

1. PARSE: Extract artist/song/album from message using Groq AI

2. ARTIST CORRECTION: Fuzzy match artist against library to fix typos
   (e.g., "Living Color" -> "Living Colour")

3. ALBUM LOOKUP: If song provided without album, query Discogs for album name
   (e.g., "Two Headed Boy" -> "In the Aeroplane Over the Sea")

4. PRIMARY SEARCH: Search library with fallback chain:
   - artist + album (from Discogs lookup or parsed)
   - artist + song (song title might match album title)
   - artist only (fallback when song/album not found)

5. ALTERNATIVE INTERPRETATION: For ambiguous "X - Y" formats, try both:
   - X as artist, Y as title
   - Y as artist, X as title

6. COMPILATION SEARCH: If no results, cross-reference Discogs track listings
   with library to find song on compilations/soundtracks

Each step is tracked via telemetry for observability.
"""
import asyncio
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from groq import Groq
from posthog import Posthog
from pydantic import BaseModel

from artwork.models import ArtworkRequest, ArtworkResponse
from artwork.router import lookup_releases_by_artist, lookup_releases_by_track
from core.dependencies import SlackService, get_groq_client, get_library_db, get_slack_service
from core.dependencies import get_artwork_finder, get_posthog_client
from artwork.finder import ArtworkFinder
from core.matching import (
    COMPILATION_KEYWORDS,
    MAX_SEARCH_RESULTS,
    STOPWORDS,
    detect_ambiguous_format,
    is_compilation_artist,
)
from core.search import (
    build_strategies,
    execute_search_pipeline,
    get_search_type_from_state,
)
from core.telemetry import RequestTelemetry
from library.models import LibraryItem
from library.db import LibraryDB
from services.parser import MessageType, ParsedRequest, parse_request
from services.slack import build_simple_slack_blocks, build_slack_blocks

logger = logging.getLogger(__name__)


def limit_results(results: list) -> list:
    """Limit results to MAX_SEARCH_RESULTS.

    Args:
        results: List of search results

    Returns:
        First MAX_SEARCH_RESULTS items from the list
    """
    return results[:MAX_SEARCH_RESULTS]


# Friendly labels for message types in Slack
MESSAGE_TYPE_LABELS = {
    MessageType.REQUEST: "Song Request",
    MessageType.DJ_MESSAGE: "Message to DJ",
    MessageType.FEEDBACK: "Feedback",
    MessageType.OTHER: "Other",
}

router = APIRouter(tags=["request"])


class RequestBody(BaseModel):
    """Request body for song request parsing."""
    message: str
    skip_slack: bool = False


class UnifiedResponse(BaseModel):
    """Combined response from parsing, artwork lookup, and library search."""
    parsed: ParsedRequest
    artwork: Optional[ArtworkResponse] = None
    library_results: list[LibraryItem] = []


async def resolve_albums_for_track(
    parsed: ParsedRequest,
) -> tuple[list[str], bool]:
    """Resolve album names for a track if not provided.

    Searches Discogs for ALL releases containing the track, not just the first one.
    This ensures we find songs on both EPs and full albums (e.g., "Percolator" is on
    both "Noises [EP]" and "Emperor Tomato Ketchup" by Stereolab).

    Args:
        parsed: Parsed request with song/artist info

    Returns:
        Tuple of (list of album names, song_not_found_flag)
    """
    # Check if album is missing or if album == artist (parser error)
    # When the parser can't identify the album, it sometimes uses the artist name
    album_is_missing = not parsed.album
    album_is_artist = (
        parsed.album and parsed.artist
        and parsed.album.lower().strip() == parsed.artist.lower().strip()
    )

    # Only do track lookup if we have an artist - without an artist, Discogs
    # results are unreliable (e.g., "Laid Back" could match any track with that name)
    if parsed.song and parsed.artist and (album_is_missing or album_is_artist):
        if album_is_artist:
            logger.info(f"Album '{parsed.album}' appears to be artist name, looking up albums")
        try:
            # Get ALL releases containing this track, not just the first one
            releases = await lookup_releases_by_track(parsed.song, parsed.artist, limit=10)
            if releases:
                # Extract unique album names, filtering to releases by this artist
                albums = []
                artist_lower = parsed.artist.lower()
                for release_artist, album in releases:
                    # Only include releases by the requested artist (not compilations)
                    if release_artist.lower().startswith(artist_lower):
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
    artist: Optional[str],
) -> list[LibraryItem]:
    """Filter library results to only include those matching the artist.

    Requires the searched artist name to appear at the START of the result's
    artist field (case-insensitive). This prevents false positives like
    "Toy" matching "Chew Toy" while still allowing "Various" to match
    "Various Artists - Rock - D".

    Args:
        results: List of library items from search
        artist: Artist name to filter by

    Returns:
        Filtered list containing only items where artist matches
    """
    if not artist:
        return results

    artist_lower = artist.lower()
    filtered = []
    for item in results:
        item_artist = (item.artist or "").lower()
        # Check if result's artist starts with searched artist
        # This handles both exact matches and "Various Artists - ..." patterns
        if item_artist.startswith(artist_lower):
            filtered.append(item)

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
        logger.info(f"Alternative search matched both interpretations, combining results")
        seen_ids = set()
        combined = []
        for item in results1 + results2:
            if item.id not in seen_ids:
                combined.append(item)
                seen_ids.add(item.id)
        return limit_results(combined), None

    return [], None


async def search_song_as_artist(db: LibraryDB, song_as_artist: str) -> tuple[list[LibraryItem], None]:
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
    discogs_releases = await lookup_releases_by_artist(song_as_artist, limit=10)

    if not discogs_releases:
        logger.info(f"No Discogs releases found for '{song_as_artist}'")
        return [], None

    logger.info(f"Found {len(discogs_releases)} Discogs releases for '{song_as_artist}'")

    # Step 3: Cross-reference album titles with library
    seen_ids = set()
    for discogs_artist, album_title in discogs_releases:
        if not album_title:
            continue

        # Search library for this album title
        album_results = await db.search(query=album_title, limit=MAX_SEARCH_RESULTS)

        for item in album_results:
            if item.id in seen_ids:
                continue

            # Accept if it's the actual artist or a compilation
            item_artist = (item.artist or "").lower()
            if item_artist.startswith(song_as_artist.lower()) or is_compilation_artist(item_artist):
                results.append(item)
                seen_ids.add(item.id)
                logger.info(f"Found '{item.artist} - {item.title}' via Discogs cross-reference")

        if len(results) >= MAX_SEARCH_RESULTS:
            break

    if results:
        logger.info(f"Found {len(results)} results via Discogs cross-reference for '{song_as_artist}'")

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
            album_lower = album.lower()
            # Extract significant words from the Discogs album title
            album_words = set(
                w for w in re.sub(r'[^\w\s]', ' ', album_lower).split()
                if len(w) > 2
            )
            filtered_results = []
            for item in results:
                item_title_lower = (item.title or "").lower()
                # Check if the library album title shares significant words with Discogs album
                item_words = set(
                    w for w in re.sub(r'[^\w\s]', ' ', item_title_lower).split()
                    if len(w) > 2
                )
                # Require at least one significant word match (besides common words)
                common_words = album_words & item_words
                if common_words:
                    filtered_results.append(item)
            results = filtered_results

            # Add unique results
            for item in results:
                if item.id not in seen_ids:
                    seen_ids.add(item.id)
                    all_results.append(item)

        if all_results:
            # Sort to prioritize results matching the first (primary) album
            primary_album_lower = albums[0].lower()
            all_results.sort(
                key=lambda r: primary_album_lower in (r.title or "").lower(),
                reverse=True,
            )
            return all_results, False

    # If no albums from Discogs, try artist + song
    if parsed.artist and parsed.song:
        query = f"{parsed.artist} {parsed.song}"
        results = await db.search(query=query, limit=MAX_SEARCH_RESULTS)
        results = filter_results_by_artist(results, parsed.artist)

        if results:
            # Prioritize results where album title matches song title
            song_lower = parsed.song.lower()
            results.sort(
                key=lambda r: song_lower in (r.title or "").lower(),
                reverse=True,
            )
            return results, False

    # If still no results, try just artist
    if not all_results and parsed.artist:
        logger.info(f"No results for albums {albums}, trying artist only: '{parsed.artist}'")
        results = await db.search(query=parsed.artist, limit=MAX_SEARCH_RESULTS)
        results = filter_results_by_artist(results, parsed.artist)
        if results:
            return results, True

    return all_results, False


async def search_compilations_for_track(
    db: LibraryDB,
    parsed: ParsedRequest,
) -> tuple[list[LibraryItem], dict[int, str]]:
    """Search for track on compilation albums using Discogs and library keyword search.
    
    Args:
        db: Library database
        parsed: Parsed request with song/artist info
        
    Returns:
        Tuple of (list of matching library items, dict mapping item_id to discogs_album_title)
    """
    if not parsed.song or not parsed.artist:
        return [], {}
    
    logger.info(f"Searching for '{parsed.song}' on other releases (compilations, etc.)")
    
    results = []
    seen_ids = set()  # Track IDs to avoid duplicates
    discogs_titles: dict[int, str] = {}  # Map item ID to Discogs album title
    
    # First, try a direct library keyword search
    keyword_matches = []
    try:
        artist_words = re.sub(r'[^\w\s]', ' ', parsed.artist.lower()).split() if parsed.artist else []
        song_words = re.sub(r'[^\w\s]', ' ', parsed.song.lower()).split() if parsed.song else []

        # Filter to significant words (using shared STOPWORDS constant)
        sig_artist = [w for w in artist_words if len(w) > 3 and w not in STOPWORDS]
        sig_song = [w for w in song_words if len(w) > 3 and w not in STOPWORDS]

        # Include both artist words (max 2) and song words (max 2) to find the right album
        query_words = sig_artist[:2] + sig_song[:2]

        if query_words:
            keyword_query = ' '.join(query_words)
            logger.info(f"Trying direct keyword search: '{keyword_query}'")
            keyword_results = await db.search(query=keyword_query, limit=MAX_SEARCH_RESULTS)

            if keyword_results:
                # Filter by artist unless it's a compilation album
                filtered_results = []
                for item in keyword_results:
                    item_artist = (item.artist or "").lower()
                    if item_artist.startswith(parsed.artist.lower()):
                        filtered_results.append(item)
                    elif is_compilation_artist(item_artist):
                        # Allow Various Artists/Soundtracks/Compilation albums
                        filtered_results.append(item)

                if filtered_results:
                    logger.info(f"Found {len(filtered_results)} matches via keyword search (after artist filter)")
                    # Don't add to results yet - prefer Discogs results which know actual track listings
                    keyword_matches = filtered_results
    except Exception as e:
        logger.warning(f"Keyword search failed: {e}")
        keyword_matches = []

    # Always try Discogs track search - it knows which albums actually have the song
    # This is more reliable than keyword matching which can return wrong albums
    try:
        # Extract full song name with remix/version info
        raw_lower = parsed.raw_message.lower()
        song_search = parsed.song

        remix_match = re.search(r'\((.*?(?:remix|mix|version|edit).*?)\)', raw_lower, re.IGNORECASE)
        if remix_match and parsed.song.lower() in raw_lower:
            song_search = f"{parsed.song} ({remix_match.group(1)})"
            logger.info(f"Using full track name with version info: '{song_search}'")

        releases = await lookup_releases_by_track(song_search, parsed.artist)
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
                filtered_matches = []
                artist_lower = parsed.artist.lower()

                # Check if the Discogs release is by a compilation artist
                discogs_is_compilation = is_compilation_artist(release_artist)

                for match in matches:
                    match_artist = (match.artist or "").lower()

                    # If Discogs says it's by the artist, only match artist albums
                    # If Discogs says it's a compilation, allow compilation matches
                    if match_artist.startswith(artist_lower):
                        filtered_matches.append(match)
                    elif discogs_is_compilation and is_compilation_artist(match_artist):
                        filtered_matches.append(match)
                matches = filtered_matches

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

    # If Discogs didn't find anything, fall back to keyword matches
    if not results and keyword_matches:
        logger.info("Discogs search found nothing, using keyword matches as fallback")
        for item in keyword_matches[:1]:
            if item.id not in seen_ids:
                results.append(item)
                seen_ids.add(item.id)

    # Prioritize albums whose title matches the song title
    # (e.g., "Meet Me in the City" album for song "Meet Me in the City")
    if results and parsed.song:
        song_lower = parsed.song.lower()
        results.sort(
            key=lambda r: song_lower in (r.title or "").lower(),
            reverse=True,
        )

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
        # Extract significant keywords (using shared STOPWORDS constant)
        words = re.sub(r'[^\w\s]', ' ', album_title.lower()).split()
        significant_words = [w for w in words if len(w) > 3 and w not in STOPWORDS]

        if significant_words:
            fuzzy_query = ' '.join(significant_words[:4])
            logger.info(f"Exact match failed for '{album_title}', trying fuzzy: '{fuzzy_query}'")
            results = await db.search(query=fuzzy_query, limit=MAX_SEARCH_RESULTS)

            # Filter results using both keyword matching AND overall similarity
            if results:
                album_lower = album_title.lower()
                filtered_results = []
                for result in results:
                    result_title_lower = (result.title or '').lower()

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


async def fetch_artwork_for_items(
    items: list[LibraryItem],
    finder: Optional[ArtworkFinder],
    discogs_titles: Optional[dict[int, str]] = None,
) -> list[tuple[LibraryItem, Optional[ArtworkResponse]]]:
    """Fetch artwork for multiple library items in parallel.
    
    Args:
        items: List of library items
        finder: Artwork finder instance
        discogs_titles: Optional dict mapping item_id to Discogs album title
        
    Returns:
        List of (item, artwork) tuples
    """
    if not finder:
        return [(item, None) for item in items]
    
    discogs_titles = discogs_titles or {}
    
    async def fetch_one(item: LibraryItem) -> Optional[ArtworkResponse]:
        try:
            # Use Discogs album title if we have it (from compilation search)
            album = discogs_titles.get(item.id, item.title)
            
            # For compilations, simplify artist to "Various" for Discogs lookup
            # Library formats like "Various Artists - Rock - C" or "Soundtracks - M" won't match Discogs
            artist = item.artist or ""
            if is_compilation_artist(artist):
                artist = "Various"
            
            result = await finder.find(ArtworkRequest(
                album=album,
                artist=artist,
            ))
            return result
        except Exception as e:
            logger.warning(f"Artwork lookup failed for {item.title}: {e}")
            return None
    
    artwork_results = await asyncio.gather(*[fetch_one(item) for item in items])
    return list(zip(items, artwork_results))


def build_context_message(
    parsed: ParsedRequest,
    found_on_compilation: bool,
    song_not_found: bool,
    has_results: bool = True,
) -> Optional[str]:
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
        return f"Found \"{parsed.song}\" by {parsed.artist} on:"

    if song_not_found and has_results:
        # Show "here are other albums" only if we have results to show
        if parsed.song and parsed.album:
            return f"\"{parsed.album}\" not found in the library, but here are other albums by {parsed.artist}:"
        elif parsed.song:
            return f"\"{parsed.song}\" is not on any album in the library, but here are some albums by {parsed.artist}:"
    elif song_not_found and not has_results:
        # No results at all after filtering
        if parsed.song and parsed.artist:
            return f"\"{parsed.song}\" by {parsed.artist} not found in library."

    return None


async def post_results_to_slack(
    slack_service: Optional[SlackService],
    message: str,
    parsed: ParsedRequest,
    items_with_artwork: list[tuple[LibraryItem, Optional[ArtworkResponse]]],
    context: Optional[str] = None,
) -> None:
    """Post formatted results to Slack.
    
    Args:
        slack_service: Slack service instance
        message: Original request message
        parsed: Parsed request
        items_with_artwork: Library items with their artwork
        context: Optional context message
        
    Raises:
        HTTPException: If posting to Slack fails
    """
    if not slack_service:
        logger.info("Slack integration disabled, skipping post")
        return
    
    if items_with_artwork:
        blocks = build_slack_blocks(message, items_with_artwork, context)
    elif not parsed.is_request:
        label = MESSAGE_TYPE_LABELS.get(parsed.message_type, "Other")
        blocks = build_simple_slack_blocks(message, f"_{label}_")
    else:
        # Request but no results found
        context_parts = []
        if parsed.artist:
            context_parts.append(f"Artist: {parsed.artist}")
        if parsed.album:
            context_parts.append(f"Album: {parsed.album}")
        if parsed.song:
            context_parts.append(f"Song: {parsed.song}")
        ctx = " | ".join(context_parts) if context_parts else None
        blocks = build_simple_slack_blocks(message, f"_No results found_ {ctx or ''}")
    
    try:
        await slack_service.post_blocks(blocks)
    except Exception as e:
        logger.error(f"Failed to post to Slack: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to post to Slack: {e}")


@router.post(
    "/request",
    response_model=UnifiedResponse,
    summary="Process song request",
    description="""
    Complete workflow: parse song request, search library, find artwork, and post to Slack.
    
    This endpoint:
    1. Parses the message using AI to extract song/album/artist
    2. Searches the library catalog for matches
    3. Fetches album artwork from external providers
    4. Posts enriched results to Slack
    5. Returns combined results
    
    Example request:
    ```json
    {
        "message": "Play Bohemian Rhapsody by Queen"
    }
    ```
    
    The response includes:
    - Parsed metadata (song, artist, album, request type)
    - Library search results with catalog info
    - Album artwork URLs where available
    """,
    responses={
        200: {"description": "Request processed successfully"},
        400: {"description": "Invalid request (empty message)"},
        500: {"description": "Processing error"},
        502: {"description": "Failed to post to Slack"},
    },
)
async def handle_request(
    request: RequestBody,
    groq_client: Groq = Depends(get_groq_client),
    db: LibraryDB = Depends(get_library_db),
    finder: Optional[ArtworkFinder] = Depends(get_artwork_finder),
    slack_service: Optional[SlackService] = Depends(get_slack_service),
    posthog_client: Optional[Posthog] = Depends(get_posthog_client),
):
    """
    Unified endpoint: parse a song request, find artwork, search the library, and post to Slack.

    1. Parses the message to extract song/album/artist
    2. Searches the library catalog
    3. Fetches artwork for each library result in parallel
    4. Posts enriched results to Slack
    5. Returns combined results
    """
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    # Initialize telemetry
    telemetry = RequestTelemetry()
    search_type = "none"

    try:
        # Step 1: Parse the message
        with telemetry.track_step("parse"):
            telemetry.record_api_call("groq")
            parsed = parse_request(request.message, groq_client)
            logger.info(f"Parsed request: is_request={parsed.is_request}, type={parsed.message_type}")

        library_results: list[LibraryItem] = []
        items_with_artwork: list[tuple[LibraryItem, Optional[ArtworkResponse]]] = []
        song_not_found = False
        found_on_compilation = False
        discogs_titles: dict[int, str] = {}

        # Step 1b: Correct artist spelling (e.g., "Living Color" -> "Living Colour")
        if parsed.artist:
            corrected_artist = await db.find_similar_artist(parsed.artist)
            if corrected_artist:
                parsed.artist = corrected_artist

        # Step 2: If we have a song but no album, look up albums from Discogs
        with telemetry.track_step("album_lookup"):
            if parsed.song and not parsed.album:
                telemetry.record_api_call("discogs")
            albums_for_search, song_not_found = await resolve_albums_for_track(parsed)

        # Step 3: Execute search strategy pipeline
        # The pipeline tries strategies in order until results are found:
        # 1. ARTIST_PLUS_ALBUM - search by artist + album/song
        # 2. SWAPPED_INTERPRETATION - try "X - Y" as both orderings
        # 3. TRACK_ON_COMPILATION - find song on compilations via Discogs
        # 4. SONG_AS_ARTIST - try parsed song as artist (parser misidentification)
        with telemetry.track_step("library_search"):
            strategies = build_strategies(
                search_library_func=search_library_with_fallback,
                search_alternative_func=search_with_alternative_interpretation,
                search_compilations_func=search_compilations_for_track,
                search_song_as_artist_func=search_song_as_artist,
            )

            search_state = await execute_search_pipeline(
                parsed=parsed,
                db=db,
                raw_message=request.message,
                strategies=strategies,
                albums_for_search=albums_for_search,
            )

            # Extract results from state
            library_results = limit_results(search_state.results)
            song_not_found = search_state.song_not_found
            found_on_compilation = search_state.found_on_compilation
            discogs_titles = search_state.discogs_titles
            search_type = get_search_type_from_state(search_state)

            # Record Discogs API call if compilation search was used
            if found_on_compilation:
                telemetry.record_api_call("discogs")

        # Step 4: Fetch artwork for library items
        with telemetry.track_step("artwork_fetch"):
            if library_results:
                # Count Discogs API calls for artwork (one per item)
                for _ in library_results:
                    telemetry.record_api_call("discogs")
                items_with_artwork = await fetch_artwork_for_items(library_results, finder, discogs_titles)

        # Step 5: Build context and post to Slack (unless skip_slack is set)
        context = build_context_message(
            parsed, found_on_compilation, song_not_found, has_results=bool(library_results)
        )
        with telemetry.track_step("slack_post"):
            if not request.skip_slack:
                telemetry.record_api_call("slack")
                await post_results_to_slack(
                    slack_service, request.message, parsed, items_with_artwork, context
                )

        # Extract main artwork from first result
        artwork = next(
            (art for _, art in items_with_artwork if art), None
        ) if items_with_artwork else None

        # Send telemetry
        if posthog_client:
            telemetry.send_to_posthog(posthog_client, {
                "results_count": len(library_results),
                "search_type": search_type,
                "had_artist": bool(parsed.artist),
                "had_album": bool(parsed.album),
                "had_song": bool(parsed.song),
                "is_request": parsed.is_request,
                "message_type": parsed.message_type.value if parsed.message_type else None,
            })

        return UnifiedResponse(
            parsed=parsed,
            artwork=artwork,
            library_results=library_results,
        )

    except HTTPException:
        raise
    except ValueError as e:
        logger.error(f"Parsing error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
