"""Fixtures for contract parity tests between inline and delegated search pipelines."""

import logging
import os
from functools import partial

import httpx
import pytest

from tests.conftest import ServerEnvironment

logger = logging.getLogger(__name__)

# URL mapping for library-metadata-lookup service
LOOKUP_ENVIRONMENT_URLS = {
    ServerEnvironment.LOCAL: "http://localhost:8001/api/v1",
    ServerEnvironment.STAGING: "https://library-metadata-lookup-staging.up.railway.app/api/v1",
    ServerEnvironment.PRODUCTION: "https://library-metadata-lookup-production.up.railway.app/api/v1",
}


def _normalize_result(
    library_results,
    search_type,
    song_not_found,
    found_on_compilation,
    corrected_artist=None,
):
    """Normalize results from either pipeline into a comparable dict."""
    return {
        "item_ids": sorted(item.id for item in library_results),
        "search_type": search_type,
        "song_not_found": song_not_found,
        "found_on_compilation": found_on_compilation,
        "corrected_artist": corrected_artist,
    }


@pytest.fixture(scope="session")
def lookup_service_url(test_environment):
    """Resolve lookup service URL and verify it's reachable."""
    url = LOOKUP_ENVIRONMENT_URLS[test_environment]
    health_url = url.replace("/api/v1", "/health")

    try:
        response = httpx.get(health_url, timeout=10.0)
        response.raise_for_status()
    except (httpx.RequestError, httpx.HTTPStatusError) as e:
        pytest.skip(f"Lookup service unreachable at {health_url}: {e}")

    logger.info(f"Lookup service reachable at {url}")
    return url


@pytest.fixture(scope="session")
def inline_lookup():
    """Factory that calls the inline search pipeline directly.

    Bypasses Groq parsing and Slack posting. Requires library.db on disk
    and DISCOGS_TOKEN in environment.
    """
    from pathlib import Path

    if not Path("library.db").exists():
        pytest.skip("library.db not found, skipping inline lookup tests")

    if not os.environ.get("DISCOGS_TOKEN"):
        pytest.skip("DISCOGS_TOKEN not set, skipping inline lookup tests")

    from config.settings import get_settings
    from core.search import build_strategies, execute_search_pipeline, get_search_type_from_state
    from discogs.service import DiscogsService
    from library.db import LibraryDB
    from routers.request import (
        filter_results_by_track_validation,
        limit_results,
        resolve_albums_for_track,
        search_compilations_for_track,
        search_library_with_fallback,
        search_song_as_artist,
        search_with_alternative_interpretation,
    )
    from services.parser import MessageType, ParsedRequest

    settings = get_settings()

    async def _do_lookup(artist=None, song=None, album=None, raw_message=""):
        db = LibraryDB(db_path=settings.resolved_library_db_path)
        await db.connect()
        discogs_service = DiscogsService(settings.discogs_token) if settings.discogs_token else None

        try:
            # Step 1b: Artist correction
            corrected_artist = None
            if artist:
                corrected = await db.find_similar_artist(artist)
                if corrected:
                    corrected_artist = corrected
                    artist = corrected

            # Build a ParsedRequest (simulating what the parser would return)
            parsed = ParsedRequest(
                artist=artist,
                song=song,
                album=album,
                is_request=True,
                message_type=MessageType.REQUEST,
                raw_message=raw_message or f"{artist or ''} {song or ''} {album or ''}".strip(),
            )

            # Step 2: Resolve albums
            albums_for_search, song_not_found = await resolve_albums_for_track(
                parsed, discogs_service
            )

            # Step 3: Execute search pipeline
            strategies = build_strategies(
                search_library_func=search_library_with_fallback,
                search_alternative_func=search_with_alternative_interpretation,
                search_compilations_func=partial(
                    search_compilations_for_track, discogs_service=discogs_service
                ),
                search_song_as_artist_func=partial(
                    search_song_as_artist, discogs_service=discogs_service
                ),
            )

            search_state = await execute_search_pipeline(
                parsed=parsed,
                db=db,
                raw_message=parsed.raw_message,
                strategies=strategies,
                albums_for_search=albums_for_search,
            )

            library_results = limit_results(search_state.results)
            song_not_found = search_state.song_not_found
            found_on_compilation = search_state.found_on_compilation
            search_type = get_search_type_from_state(search_state)

            # Step 3b: Track validation
            if song_not_found and library_results and parsed.song and parsed.artist:
                validated = await filter_results_by_track_validation(
                    library_results, parsed.song, parsed.artist, discogs_service
                )
                if validated:
                    library_results = validated
                    song_not_found = False

            return _normalize_result(
                library_results,
                search_type,
                song_not_found,
                found_on_compilation,
                corrected_artist,
            )
        finally:
            await db.close()
            if discogs_service:
                await discogs_service.close()

    return _do_lookup


@pytest.fixture(scope="session")
def http_lookup(lookup_service_url):
    """Factory that calls the lookup service over HTTP."""

    async def _do_lookup(artist=None, song=None, album=None, raw_message=""):
        async with httpx.AsyncClient(timeout=30.0) as client:
            body = {
                "raw_message": raw_message or f"{artist or ''} {song or ''} {album or ''}".strip()
            }
            if artist:
                body["artist"] = artist
            if song:
                body["song"] = song
            if album:
                body["album"] = album

            response = await client.post(f"{lookup_service_url}/lookup", json=body)
            response.raise_for_status()
            data = response.json()

        # Normalize to same format as inline
        from library.models import LibraryItem

        items = [LibraryItem(**r["library_item"]) for r in data.get("results", [])]
        return _normalize_result(
            items,
            data.get("search_type", "none"),
            data.get("song_not_found", False),
            data.get("found_on_compilation", False),
            data.get("corrected_artist"),
        )

    return _do_lookup
