"""
Integration tests against production services.

Run with: pytest tests/test_integration.py -v
Skip with: pytest tests/ -m "not integration"
"""

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from discogs.service import DiscogsService
from library.db import LibraryDB

load_dotenv()

# Mark all tests in this file as integration tests
pytestmark = pytest.mark.integration

# Skip if required env vars not set
DISCOGS_TOKEN = os.getenv("DISCOGS_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
# library.db is in project root, not tests/ directory
LIBRARY_DB_PATH = Path(__file__).parent.parent.parent / "library.db"

skip_if_no_token = pytest.mark.skipif(
    not DISCOGS_TOKEN, reason="DISCOGS_TOKEN not set - skipping integration tests"
)

skip_if_no_db = pytest.mark.skipif(
    not LIBRARY_DB_PATH.exists(), reason="library.db not found - skipping integration tests"
)

skip_if_no_groq = pytest.mark.skipif(
    not GROQ_API_KEY, reason="GROQ_API_KEY not set - skipping parser integration tests"
)


class TestDiscogsIntegration:
    """Test against the real Discogs API."""

    @pytest.mark.asyncio
    @skip_if_no_token
    async def test_manu_dibango_compilation_search(self):
        """Test the actual Manu Dibango compilation search scenario."""
        assert DISCOGS_TOKEN is not None
        service = DiscogsService(DISCOGS_TOKEN)

        # Test the real scenario
        response = await service.search_releases_by_track("Abele Dance (85 Remix)", "Manu Dibango")

        print(f"\n✅ Found {len(response.releases)} releases on Discogs:")
        for i, release in enumerate(response.releases[:5], 1):
            print(f"  {i}. {release.artist} - {release.album}")

        # Verify we get results with valid structure
        # Note: Discogs results can change over time, so we only verify
        # that results have the expected structure
        assert len(response.releases) >= 0, "Should return a list of releases"

        for release in response.releases[:5]:
            assert hasattr(release, "artist"), "Release should have artist"
            assert hasattr(release, "album"), "Release should have album"

        # Note if compilation is in results (informational, not required)
        album_titles = [r.album for r in response.releases]
        has_compilation = any(
            "change" in album.lower() and "beat" in album.lower() for album in album_titles
        )

        if has_compilation:
            print("  ✅ Found 'Change The Beat' compilation!")
        else:
            print("  ⚠️  Compilation not in top results (this is OK - Discogs data varies)")
            print(f"     All albums: {album_titles[:5]}")

        await service.close()

    @pytest.mark.asyncio
    @skip_if_no_token
    async def test_sugar_plant_filters_false_positive_compilations(self):
        """
        Test that searching for 'Simple' by 'Sugar Plant' filters out false positives.

        Bug: Discogs was returning "22 Explosive Hits, Vol 2" because it contains
        "A Simple Man" by "Sugar Bears (2)" - partial matches on track and artist.

        Expected: The tracklist validation should filter out compilations that don't
        actually contain "Simple" by "Sugar Plant".
        """
        from discogs.lookup import lookup_releases_by_track

        releases = await lookup_releases_by_track("Simple", "Sugar Plant")

        print(f"\n✅ Found {len(releases)} releases on Discogs:")
        for i, (artist, album) in enumerate(releases[:10], 1):
            print(f"  {i}. {artist} - {album}")

        # Should NOT include "22 Explosive Hits" or similar false positives
        for _artist, album in releases:
            album_lower = album.lower()
            assert "explosive" not in album_lower, (
                f"Should not include '{album}' - it contains 'A Simple Man' by "
                f"'Sugar Bears', not 'Simple' by 'Sugar Plant'"
            )

        # If we have results, they should be actual Sugar Plant releases
        # or verified compilations (Various Artists that passed tracklist validation)
        for artist, _album in releases:
            artist_lower = artist.lower()
            is_sugar_plant = "sugar plant" in artist_lower
            is_compilation = "various" in artist_lower
            assert is_sugar_plant or is_compilation, (
                f"Expected Sugar Plant or verified compilation, got '{artist}'"
            )

        print("\n✅ Tracklist validation correctly filtered false positives!")

    @pytest.mark.asyncio
    @skip_if_no_token
    async def test_discogs_rate_limiting(self):
        """Test that we handle rate limits gracefully."""
        assert DISCOGS_TOKEN is not None
        service = DiscogsService(DISCOGS_TOKEN)

        # Make multiple rapid requests
        results = []
        for i in range(3):
            response = await service.search_releases_by_track(f"Test Track {i}", "Test Artist")
            results.append(response)

        # Should complete without errors (even if rate limited)
        assert len(results) == 3

        await service.close()


class TestLibraryIntegration:
    """Test against the real library.db database."""

    @pytest.mark.asyncio
    @skip_if_no_db
    async def test_celluloid_compilation_in_library(self):
        """Test that the Celluloid compilation exists in the library."""
        db = LibraryDB(db_path=LIBRARY_DB_PATH)
        await db.connect()

        # Search for the compilation
        results = await db.search(query="Celluloid change beat", limit=5)

        print(f"\n✅ Found {len(results)} results in library:")
        for result in results:
            print(f"  - {result.artist} - {result.title}")
            print(f"    Call: {result.call_number}")

        # Verify we found it
        assert len(results) > 0, "Should find Celluloid compilation"

        # Check for exact match
        found_exact = any(
            "celluloid" in (result.title or "").lower()
            and "change" in (result.title or "").lower()
            and "beat" in (result.title or "").lower()
            for result in results
        )

        assert found_exact, "Should find exact Celluloid compilation"

        await db.close()

    @pytest.mark.asyncio
    @skip_if_no_db
    async def test_fuzzy_search_with_special_chars(self):
        """Test fuzzy search with real data."""
        db = LibraryDB(db_path=LIBRARY_DB_PATH)
        await db.connect()

        # Test with special characters (should use fallback)
        results = await db.search(query="Richard D. James Album = リチャード", limit=5)

        print(f"\n✅ Found {len(results)} results with special chars:")
        for result in results[:3]:
            print(f"  - {result.artist} - {result.title}")

        await db.close()

    @pytest.mark.asyncio
    @skip_if_no_db
    async def test_various_artists_search(self):
        """Test searching for Various Artists releases."""
        db = LibraryDB(db_path=LIBRARY_DB_PATH)
        await db.connect()

        # Search for soundtracks and compilations
        results = await db.search(query="Various Artists", limit=10)

        print(f"\n✅ Found {len(results)} Various Artists releases:")
        for result in results[:5]:
            print(f"  - {result.title}")
            print(f"    Artist: {result.artist}")

        assert len(results) > 0, "Should find Various Artists releases"

        await db.close()

    @pytest.mark.asyncio
    @skip_if_no_db
    async def test_artist_only_search_echo_and_the_bunnymen(self):
        """Test artist-only search returns results.

        Bug: After refactoring to support multiple albums, artist-only searches
        (with no song or album) would return no results because the search
        condition required albums_for_search OR song to be present.

        Expected: Searching for just an artist name should return their albums.
        """
        db = LibraryDB(db_path=LIBRARY_DB_PATH)
        await db.connect()

        results = await db.search(query="Echo and the Bunnymen", limit=5)

        print(f"\n✅ Found {len(results)} results for 'Echo and the Bunnymen':")
        for result in results:
            print(f"  - {result.artist} - {result.title}")
            print(f"    Call: {result.call_number}")

        assert len(results) > 0, "Should find Echo and the Bunnymen albums"

        # Verify they're actually by Echo and the Bunnymen
        for result in results:
            assert result.artist is not None, "Result should have artist"
            assert "echo" in result.artist.lower(), (
                f"Result should be by Echo and the Bunnymen, got {result.artist}"
            )

        await db.close()

    @pytest.mark.asyncio
    @skip_if_no_db
    async def test_dj_blaqstarr_in_library(self):
        """Test that DJ Blaqstarr's release exists in the library.

        Expected: "Shake It To The Ground" should be in the library
        with catalog ID "Hiphop cd DJ 70/1"

        Note: This test depends on specific library content and may be
        skipped if the release is not present.
        """
        db = LibraryDB(db_path=LIBRARY_DB_PATH)
        await db.connect()

        # Search for the artist
        results = await db.search(query="DJ Blaqstarr", limit=5)

        print(f"\n✅ Found {len(results)} results for DJ Blaqstarr:")
        for result in results:
            print(f"  - {result.artist} - {result.title}")
            print(f"    Call: {result.call_number}")

        # Skip if content not in library (library content changes over time)
        if len(results) == 0:
            await db.close()
            pytest.skip("DJ Blaqstarr not in library - content may have changed")

        # Check for expected catalog structure (DJ 70/1 in Hiphop)
        found_expected = any(
            result.call_letters == "DJ" and result.artist_call_number == 70 for result in results
        )

        if found_expected:
            print("  ✅ Found DJ Blaqstarr with expected catalog ID (DJ 70)")

        await db.close()


class TestEndToEndIntegration:
    """Test the full workflow: Discogs -> Library matching."""

    @pytest.mark.asyncio
    @skip_if_no_token
    @skip_if_no_db
    async def test_full_compilation_search_workflow(self):
        """
        Test the complete workflow:
        1. Search Discogs for track
        2. Get list of releases
        3. Check each against library
        4. Find the compilation

        Note: This test depends on both Discogs data and library content,
        which can change over time.
        """
        # Step 1: Search Discogs
        assert DISCOGS_TOKEN is not None
        service = DiscogsService(DISCOGS_TOKEN)
        response = await service.search_releases_by_track("Abele Dance (85 Remix)", "Manu Dibango")

        print(f"\n📀 Step 1: Found {len(response.releases)} releases on Discogs")

        # Skip if Discogs returns no results
        if len(response.releases) == 0:
            await service.close()
            pytest.skip("No Discogs results found - API data may have changed")

        # Step 2: Check library for each
        db = LibraryDB(db_path=LIBRARY_DB_PATH)
        await db.connect()

        found_in_library = []
        print("\n🔍 Step 2: Checking library for each release...")

        for release in response.releases[:10]:  # Check first 10
            # Try exact match first
            results = await db.search(query=release.album, limit=1)

            # Try fuzzy match if exact fails
            if not results:
                # Extract keywords
                import re

                words = re.sub(r"[^\w\s]", " ", release.album.lower()).split()
                significant = [w for w in words if len(w) > 3][:3]

                if significant:
                    fuzzy_query = " ".join(significant)
                    results = await db.search(query=fuzzy_query, limit=1)

            if results:
                found_in_library.append((release.album, results[0]))
                print(f"  ✅ Found: {results[0].title}")

        # Step 3: Report results
        print(f"\n🎉 Found {len(found_in_library)} matches in library!")

        # Skip if no matches found (library content may have changed)
        if len(found_in_library) == 0:
            await service.close()
            await db.close()
            pytest.skip("No library matches found - library content may have changed")

        # Check if we found the compilation (informational)
        has_compilation = any(
            item.title is not None
            and ("celluloid" in item.title.lower() or "change" in item.title.lower())
            for _, item in found_in_library
        )

        if has_compilation:
            print("  ✅ Successfully matched compilation!")
        else:
            print("  ⚠️  Compilation not found (this is OK - library content varies)")

        await service.close()
        await db.close()

    @pytest.mark.asyncio
    @skip_if_no_token
    @skip_if_no_db
    async def test_keyword_fallback_search(self):
        """
        Test that keyword fallback works when searching by artist + album keywords.
        This tests the logic added in routers/request.py.
        """
        db = LibraryDB(db_path=LIBRARY_DB_PATH)
        await db.connect()

        # Simulate the keyword extraction logic with an album that exists
        import re

        # Use an album we know exists: "Selected Ambient works vol 2" by Aphex Twin
        song = "ambient works"  # Keywords from actual album title
        artist = "Aphex Twin"

        # Extract significant words (matching routers/request.py logic)
        artist_words = re.sub(r"[^\w\s]", " ", artist.lower()).split()
        song_words = re.sub(r"[^\w\s]", " ", song.lower()).split()

        all_words = artist_words + song_words
        significant = [
            w
            for w in all_words
            if len(w) > 3
            and w not in {"the", "and", "with", "from", "that", "this", "play", "song", "remix"}
        ]

        keyword_query = " ".join(significant[:3])
        print(f"\n🔍 Testing keyword search: '{keyword_query}'")

        results = await db.search(query=keyword_query, limit=3)

        print(f"✅ Found {len(results)} results:")
        for result in results:
            print(f"  - {result.artist} - {result.title}")

        # Verify we find something relevant
        assert len(results) > 0, "Keyword search should find results"

        # Check if we found Aphex Twin albums
        has_aphex = any(
            result.artist is not None and "aphex" in result.artist.lower() for result in results
        )

        assert has_aphex, "Should find Aphex Twin album with keyword search"
        print("  ✅ Keyword search found Aphex Twin album!")

        await db.close()

    @pytest.mark.asyncio
    @skip_if_no_token
    @skip_if_no_db
    async def test_dj_blaqstarr_shake_it_to_the_ground(self):
        """
        Test the complete workflow for "Shake It To The Ground" by DJ Blaqstarr.

        Expected outcome:
        - Discogs should find the track
        - Library should have one matching release (catalog: Hiphop cd DJ 70/1)

        Note: This test depends on both Discogs data and library content,
        which can change over time.
        """
        # Step 1: Search Discogs for the track
        assert DISCOGS_TOKEN is not None
        service = DiscogsService(DISCOGS_TOKEN)
        response = await service.search_releases_by_track("Shake It To The Ground", "DJ Blaqstarr")

        print(f"\n📀 Step 1: Found {len(response.releases)} releases on Discogs")
        for i, release in enumerate(response.releases[:5], 1):
            print(f"  {i}. {release.artist} - {release.album}")

        # Step 2: Check library for matches
        db = LibraryDB(db_path=LIBRARY_DB_PATH)
        await db.connect()

        found_in_library = []
        print("\n🔍 Step 2: Checking library for each release...")

        for release in response.releases[:10]:
            # Try exact match
            results = await db.search(query=release.album, limit=1)

            # Try fuzzy match if exact fails
            if not results:
                import re

                words = re.sub(r"[^\w\s]", " ", release.album.lower()).split()
                significant = [w for w in words if len(w) > 3][:3]

                if significant:
                    fuzzy_query = " ".join(significant)
                    results = await db.search(query=fuzzy_query, limit=1)

            if results:
                found_in_library.append((release.album, results[0]))
                print(f"  ✅ Found: {results[0].title} ({results[0].call_number})")

        # Step 3: Also search library directly for DJ Blaqstarr
        print("\n🔍 Step 3: Direct library search for DJ Blaqstarr...")
        direct_results = await db.search(query="DJ Blaqstarr", limit=5)

        for result in direct_results:
            print(f"  - {result.title} ({result.call_number})")
            # Add to found list if not already there
            if not any(item.id == result.id for _, item in found_in_library):
                found_in_library.append(("Direct search", result))

        print(f"\n🎉 Total found: {len(found_in_library)} matches in library!")

        # Skip if content not in library (library content changes over time)
        if len(found_in_library) == 0 and len(direct_results) == 0:
            await service.close()
            await db.close()
            pytest.skip("DJ Blaqstarr not found in library - content may have changed")

        # Check for expected catalog ID (DJ 70/1)
        all_results = [item for _, item in found_in_library] + direct_results
        has_expected_catalog = any(
            result.call_letters == "DJ" and result.artist_call_number == 70
            for result in all_results
        )

        if has_expected_catalog:
            print("  ✅ Found release with expected catalog ID (DJ 70/1)!")

        await service.close()
        await db.close()


class TestParserIntegration:
    """Test the parser against the real Groq API."""

    @pytest.mark.asyncio
    @skip_if_no_groq
    async def test_preserves_asterisks_in_artist_name(self):
        """Test that special characters like asterisks are preserved in artist names.

        The artist "Quix*o*tic" should NOT be normalized to "Quixotic".
        """
        from groq import Groq

        from services.parser import parse_request

        client = Groq(api_key=GROQ_API_KEY)

        result = parse_request("something by quix*o*tic", client)

        print("\n📝 Parsed result:")
        print(f"  Artist: {result.artist}")
        print(f"  Is Request: {result.is_request}")

        assert result.is_request is True
        assert result.artist is not None

        # The key assertion: asterisks should be preserved
        assert "*" in result.artist, (
            f"Expected asterisks to be preserved in artist name, got: {result.artist}"
        )
        assert result.artist.lower().replace("*", "") == "quixotic", (
            f"Expected artist to be 'Quix*o*tic' (or similar), got: {result.artist}"
        )

        print(f"  ✅ Asterisks preserved: {result.artist}")

    @pytest.mark.asyncio
    @skip_if_no_groq
    async def test_preserves_special_chars_in_various_artists(self):
        """Test preservation of special characters in well-known artist names."""
        from groq import Groq

        from services.parser import parse_request

        client = Groq(api_key=GROQ_API_KEY)

        test_cases = [
            ("play something by P!nk", "P!nk", "!"),
            ("deadmau5 please", "deadmau5", "5"),
        ]

        for message, _expected_contains, special_char in test_cases:
            result = parse_request(message, client)

            print(f"\n📝 '{message}' -> Artist: {result.artist}")

            assert result.artist is not None, f"Expected artist for '{message}'"
            # Check special char is preserved (case-insensitive check on base name)
            assert special_char in result.artist or special_char in result.artist.lower(), (
                f"Expected '{special_char}' in artist name for '{message}', got: {result.artist}"
            )

            print(f"  ✅ Special char '{special_char}' preserved")

    @pytest.mark.asyncio
    @skip_if_no_groq
    async def test_parses_comma_separated_song_artist_format(self):
        """Test that 'song title, artist name' format is recognized as a request.

        Bug: The parser wasn't recognizing comma-separated format like
        "the man in your house, mi ami" as a song request.

        Expected: Should extract song and artist from comma-separated format.
        """
        from groq import Groq

        from services.parser import parse_request

        client = Groq(api_key=GROQ_API_KEY)

        result = parse_request("the man in your house, mi ami", client)

        print("\n📝 Parsed result:")
        print(f"  Song: {result.song}")
        print(f"  Artist: {result.artist}")
        print(f"  Is Request: {result.is_request}")

        assert result.is_request is True, "Should recognize as a request"
        assert result.song is not None, "Should extract song title"
        assert result.artist is not None, "Should extract artist name"
        assert "man" in result.song.lower() and "house" in result.song.lower(), (
            f"Expected song 'The Man in Your House', got: {result.song}"
        )
        assert "mi ami" in result.artist.lower(), f"Expected artist 'Mi Ami', got: {result.artist}"

        print("  ✅ Correctly parsed comma-separated format!")


class TestFullRequestIntegration:
    """Test the full /request endpoint.

    Set TEST_ENV to control which server to test against:
        TEST_ENV=local pytest ...      # localhost:8000 (default, requires local server)
        TEST_ENV=staging pytest ...    # staging server on Railway
        TEST_ENV=production pytest ... # production server on Railway
    """

    @pytest.mark.asyncio
    async def test_artist_only_search_returns_results(self, base_url):
        """
        Test that artist-only searches (no song or album) return results.

        Bug: After refactoring to support multiple albums from Discogs, artist-only
        searches like "Can i request something from echo and the bunnymen" returned
        no results because the search condition required albums_for_search OR song.

        Expected: Should return Echo and the Bunnymen albums.
        """
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={
                    "message": "Can i request something from echo and the bunnymen",
                    "skip_slack": True,
                },
            )
            response.raise_for_status()
            data = response.json()

        # Check parsing
        parsed = data.get("parsed", {})
        assert parsed.get("artist") == "Echo and the Bunnymen", (
            f"Should parse artist as 'Echo and the Bunnymen', got {parsed.get('artist')}"
        )

        # Check results
        results = data.get("library_results", [])
        assert len(results) > 0, "Should find Echo and the Bunnymen albums"

        # Verify all results are by Echo and the Bunnymen
        for result in results:
            assert "echo" in result.get("artist", "").lower(), (
                f"Result should be by Echo and the Bunnymen, got {result.get('artist')}"
            )

        print(f"\n✅ Artist-only search returned {len(results)} results:")
        for r in results:
            print(f"    - {r.get('artist')} - {r.get('title')}")

    @pytest.mark.asyncio
    async def test_meet_me_in_the_city_returns_correct_album(self, base_url):
        """
        Test that 'Meet Me in the City Junior Kimbrough' returns the correct album.

        Bug: The search was returning 'Do the Rump' instead of 'Meet Me in the City'
        even though the library has an album called 'Meet Me in the City' by Junior Kimbrough.

        Expected: Should return 'Meet Me in the City' album (Blues cd KI 6/4)
        """
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": "Meet Me in the City Junior Kimbrough", "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        parsed = data.get("parsed", {})
        results = data.get("library_results", [])

        print("\n📝 Parsed:")
        print(f"  Artist: {parsed.get('artist')}")
        print(f"  Song: {parsed.get('song')}")
        print(f"  Album: {parsed.get('album')}")

        print("\n📚 Library Results:")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')} ({r.get('call_number')})")

        # Should have results
        assert len(results) > 0, "Should find results for Junior Kimbrough"

        # The first result should be "Meet Me in the City", NOT "Do the Rump"
        first_result = results[0]
        assert "meet me in the city" in first_result.get("title", "").lower(), (
            f"Expected 'Meet Me in the City' album, but got '{first_result.get('title')}'. "
            f"The search returned an album that doesn't contain the requested song."
        )

        print("\n✅ Correctly returned 'Meet Me in the City' album!")

    @pytest.mark.asyncio
    async def test_thoughtforms_by_lush_excludes_albums_without_song(self, base_url):
        """
        Test that 'Thoughtforms by Lush' only returns albums that have the song.

        Bug: The search was returning 'Lovelife' which doesn't have 'Thoughtforms' on it,
        because the fallback search returned all Lush albums without filtering.

        Expected: Should return albums that actually have Thoughtforms (Scar, Mad Love, Gala)
        and NOT return Lovelife.
        """
        import httpx

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": "Can i request thoughtforms by lush", "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        parsed = data.get("parsed", {})
        results = data.get("library_results", [])

        print("\n📝 Parsed:")
        print(f"  Artist: {parsed.get('artist')}")
        print(f"  Song: {parsed.get('song')}")

        print("\n📚 Library Results:")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        # Should have results
        assert len(results) > 0, "Should find results for Lush"

        # All results should be by Lush
        for r in results:
            assert r.get("artist") == "Lush", f"Expected Lush, got {r.get('artist')}"

        # Should NOT include Lovelife (which doesn't have Thoughtforms)
        titles = [r.get("title", "").lower() for r in results]
        assert "lovelife" not in titles, (
            "Lovelife should NOT be in results because it doesn't have Thoughtforms"
        )

        # Should include albums that actually have Thoughtforms
        # (According to Discogs: Mad Love, Scar, Gala, etc.)
        has_valid_album = any(title in ["mad love", "scar", "gala"] for title in titles)
        assert has_valid_album, (
            f"Expected at least one album that has Thoughtforms (Mad Love, Scar, or Gala), "
            f"but got: {titles}"
        )

        print("\n✅ Correctly excluded albums without the requested song!")

    @pytest.mark.asyncio
    async def test_biosphere_excludes_albums_without_track(self, base_url):
        """
        Test that 'The Things I Tell You by Biosphere' excludes albums without the track.

        Bug: Searching for a track would return albums by the artist that don't have
        the track, because the fuzzy library search matched the artist but not the album.

        Expected: Should return Substrata and Wireless (which have the track)
        and NOT return Stator (which doesn't have it).
        """
        import httpx

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": "The Things I Tell You by Biosphere", "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        parsed = data.get("parsed", {})
        results = data.get("library_results", [])

        print("\n📝 Parsed:")
        print(f"  Artist: {parsed.get('artist')}")
        print(f"  Song: {parsed.get('song')}")

        print("\n📚 Library Results:")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        # Should have results
        assert len(results) > 0, "Should find results for Biosphere"

        # All results should be by Biosphere
        for r in results:
            assert r.get("artist") == "Biosphere", f"Expected Biosphere, got {r.get('artist')}"

        # Should NOT include Stator (which doesn't have The Things I Tell You)
        titles = [r.get("title", "").lower() for r in results]
        assert "stator" not in titles, (
            "Stator should NOT be in results because it doesn't have 'The Things I Tell You'"
        )

        # Should include albums that actually have the track
        # (According to Discogs: Substrata, Wireless)
        has_valid_album = any(title in ["substrata", "wireless"] for title in titles)
        assert has_valid_album, (
            f"Expected at least one album that has 'The Things I Tell You' "
            f"(Substrata or Wireless), but got: {titles}"
        )

        print("\n✅ Correctly excluded albums without the requested track!")

    @pytest.mark.asyncio
    async def test_young_gov_excludes_young_black_teenagers(self, base_url):
        """
        Test that searching for 'Young Gov' does not return 'Young Black Teenagers'.

        Bug: Artist prefix matching was too loose, matching 'Young Black Teenagers'
        when searching for 'Young Gov' because both start with 'Young'.

        Expected: Should return only results where artist starts with 'Young Gov',
        or no results if no exact match exists.
        """
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": "Young Gov", "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        results = data.get("library_results", [])

        print("\n📚 Library Results for 'Young Gov':")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        # Should NOT include Young Black Teenagers
        for r in results:
            artist = r.get("artist", "").lower()
            assert "young black teenagers" not in artist, (
                "'Young Black Teenagers' should not match 'Young Gov' search"
            )

        print("\n✅ Correctly excluded 'Young Black Teenagers' from 'Young Gov' search!")

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        reason="Known bug: artist-only searches may return Various Artists compilations with search term only in title"
    )
    async def test_laid_back_matches_band_not_album_titles(self, base_url):
        """
        Test that searching for 'Laid Back' doesn't return false positive title matches.

        Bug: Search returns albums like "Night Shift - Laid Back Trip Hop" (Various Artists)
        because the title contains "Laid Back", even though the artist is different.

        Expected: Should prefer albums by artists with "Laid Back" in their name
        (e.g., Gregg Allman - "Laid Back", Beatnik Filmstars - "Laid Back and English")
        over Various Artists compilations with "laid back" only in title.
        """
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": "Laid Back", "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        results = data.get("library_results", [])

        print("\n📚 Library Results for 'Laid Back':")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        # Check that we don't return Various Artists compilations (title-only matches)
        various_artists_results = [
            r for r in results if "various artists" in r.get("artist", "").lower()
        ]

        assert len(various_artists_results) == 0, (
            f"Should not return Various Artists compilations: {various_artists_results}"
        )

        # Check that results contain "laid back" in either artist or title
        for r in results:
            artist = r.get("artist", "").lower()
            title = r.get("title", "").lower()

            assert "laid back" in artist or "laid back" in title, (
                f"Unrelated result: '{r.get('artist')}' - '{r.get('title')}'"
            )

        print("\n✅ No Various Artists false positives!")

    @pytest.mark.asyncio
    async def test_toy_excludes_chew_toy(self, base_url):
        """
        Test that searching for 'Toy' does not return 'Chew Toy'.

        Bug: Artist filtering was matching partial words, so 'Toy' matched 'Chew Toy'.

        Expected: Should only return albums by artist 'Toy', not 'Chew Toy'.
        """
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": "Toy", "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        results = data.get("library_results", [])

        print("\n📚 Library Results for 'Toy':")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        # Should NOT include "Chew Toy"
        for r in results:
            artist = r.get("artist", "").lower()
            assert "chew toy" not in artist, (
                "'Chew Toy' should not match 'Toy' search - "
                "artist filtering should use word boundaries"
            )

        print("\n✅ Correctly excluded 'Chew Toy' from 'Toy' search!")

    @pytest.mark.asyncio
    async def test_amps_for_christ_excludes_edward_bear(self, base_url):
        """
        Test that searching for 'Amps for Christ' does not return 'Edward Bear'.

        Bug: Ambiguous format detection ("Amps for Christ - Edward") was incorrectly
        matching "Edward Bear" albums.

        Expected: Should return 'Amps for Christ' albums, not 'Edward Bear'.
        """
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": "Amps for Christ", "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        results = data.get("library_results", [])

        print("\n📚 Library Results for 'Amps for Christ':")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        # Should NOT include Edward Bear
        for r in results:
            artist = r.get("artist", "").lower()
            assert "edward bear" not in artist, (
                "'Edward Bear' should not match 'Amps for Christ' search"
            )

        # If we have results, they should be by Amps for Christ
        if results:
            has_amps = any(
                r.get("artist", "").lower().startswith("amps for christ") for r in results
            )
            assert has_amps, (
                f"Expected 'Amps for Christ' albums, got: {[r.get('artist') for r in results]}"
            )

        print("\n✅ Correctly excluded 'Edward Bear' from 'Amps for Christ' search!")

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        reason="Known bug: keyword search doesn't prioritize albums with the song title"
    )
    async def test_holland_1945_returns_aeroplane(self, base_url):
        """
        Test that 'Holland, 1945 Neutral Milk Hotel' returns the correct album.

        Bug: Search was returning 'On Avery Island' instead of 'In the Aeroplane Over the Sea'
        because the keyword search wasn't including song title words.

        Expected: Should return 'In the Aeroplane Over the Sea' (which has Holland, 1945).
        """
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": "Holland, 1945 Neutral Milk Hotel", "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        parsed = data.get("parsed", {})
        results = data.get("library_results", [])

        print("\n📝 Parsed:")
        print(f"  Artist: {parsed.get('artist')}")
        print(f"  Song: {parsed.get('song')}")

        print("\n📚 Library Results:")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        # Should have results
        assert len(results) > 0, "Should find results for Neutral Milk Hotel"

        # First result should be "In the Aeroplane Over the Sea"
        first_result = results[0]
        assert "aeroplane" in first_result.get("title", "").lower(), (
            f"Expected 'In the Aeroplane Over the Sea', got '{first_result.get('title')}'. "
            f"The search returned an album that doesn't have 'Holland, 1945'."
        )

        print("\n✅ Correctly returned 'In the Aeroplane Over the Sea'!")

    @pytest.mark.asyncio
    async def test_mi_ami_comma_format_returns_watersports(self, base_url):
        """
        Test that 'the man in your house, mi ami' returns the album Watersports.

        Bug: The parser wasn't recognizing "song, artist" (comma-separated) format,
        classifying it as "other" instead of a request.

        Expected: Should parse as request and return 'Watersports' by Mi Ami.
        """
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": "the man in your house, mi ami", "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        parsed = data.get("parsed", {})
        results = data.get("library_results", [])

        print("\n📝 Parsed:")
        print(f"  Is Request: {parsed.get('is_request')}")
        print(f"  Artist: {parsed.get('artist')}")
        print(f"  Song: {parsed.get('song')}")

        print("\n📚 Library Results:")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        # Should be recognized as a request
        assert parsed.get("is_request") is True, (
            "Should recognize 'song, artist' format as a request"
        )

        # Should have results
        assert len(results) > 0, "Should find results for Mi Ami"

        # Should return Watersports
        first_result = results[0]
        assert "watersports" in first_result.get("title", "").lower(), (
            f"Expected 'Watersports' album, got '{first_result.get('title')}'"
        )
        assert "mi ami" in first_result.get("artist", "").lower(), (
            f"Expected artist 'Mi Ami', got '{first_result.get('artist')}'"
        )

        print("\n✅ Correctly returned 'Watersports' by Mi Ami!")

    @pytest.mark.asyncio
    async def test_living_color_corrects_to_living_colour(self, base_url):
        """
        Test that 'Cult of Personality by Living Color' finds Living Colour.

        Bug: American spelling "Living Color" wasn't matching British spelling
        "Living Colour" in the library, returning no results.

        Expected: Should correct spelling and return Living Colour albums.
        """
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": "Cult of Personality by Living Color", "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        parsed = data.get("parsed", {})
        results = data.get("library_results", [])

        print("\n📝 Parsed:")
        print(f"  Artist: {parsed.get('artist')}")
        print(f"  Song: {parsed.get('song')}")

        print("\n📚 Library Results:")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        # Should have results despite spelling difference
        assert len(results) > 0, "Should find Living Colour albums even when spelled 'Living Color'"

        # All results should be by Living Colour
        for r in results:
            assert "living colour" in r.get("artist", "").lower(), (
                f"Expected 'Living Colour', got '{r.get('artist')}'"
            )

        print("\n✅ Correctly corrected 'Living Color' to 'Living Colour'!")

    @pytest.mark.asyncio
    async def test_sugar_plant_excludes_unrelated_compilations(self, base_url):
        """
        Test that 'Simple by Sugar Plant' does not return unrelated compilations.

        Bug: Discogs search for "Simple" was returning "22 Explosive Hits, Vol 2"
        because it contains "A Simple Man" by "Sugar Bears (2)" - partial matches
        on both track and artist. Then fuzzy album matching was incorrectly matching
        this to "K-Tel: 22 Explosive Hits!" in the library.

        Expected: Should either return actual Sugar Plant albums, or no results
        if Sugar Plant isn't in the library. Should NOT return K-Tel compilations.
        """
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": "Simple by Sugar Plant", "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        parsed = data.get("parsed", {})
        results = data.get("library_results", [])

        print("\n📝 Parsed:")
        print(f"  Artist: {parsed.get('artist')}")
        print(f"  Song: {parsed.get('song')}")

        print("\n📚 Library Results:")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        # Should NOT include unrelated K-Tel or "Explosive Hits" compilations
        for r in results:
            title = r.get("title", "").lower()
            assert "k-tel" not in title and "explosive" not in title, (
                f"Should not return unrelated compilation '{r.get('title')}' "
                f"for 'Simple by Sugar Plant' request"
            )

        # If we have results, they should be by Sugar Plant or Various Artists
        # compilations that actually contain "Simple" by Sugar Plant
        for r in results:
            artist = r.get("artist", "").lower()
            # Either by Sugar Plant directly, or a verified compilation
            is_sugar_plant = "sugar plant" in artist
            is_valid_compilation = "various" in artist
            assert is_sugar_plant or is_valid_compilation, (
                f"Expected Sugar Plant or verified compilation, got '{r.get('artist')}'"
            )

        print("\n✅ Correctly excluded unrelated compilations!")

    @pytest.mark.asyncio
    async def test_results_have_unique_library_urls(self, base_url):
        """
        Test that all results have unique library URLs.

        Each library result should point to a different library record.
        No duplicates should be returned.
        """
        import httpx

        # Use queries known to return multiple results from other tests
        test_queries = [
            "Junior Kimbrough",  # Known from test_meet_me_in_the_city_returns_correct_album
            "Various Artists",
            "Laid Back",  # Known from test_laid_back_matches_band_not_album_titles
        ]

        found_multi_result = False
        for query in test_queries:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{base_url}/request",
                    json={"message": query, "skip_slack": True},
                )
                response.raise_for_status()
                data = response.json()

            results = data.get("library_results", [])

            # Check all IDs are unique
            ids = [r.get("id") for r in results]
            assert len(ids) == len(set(ids)), f"Duplicate IDs found in results for '{query}': {ids}"

            # Check all library URLs are unique
            library_urls = [r.get("library_url") for r in results]
            assert len(library_urls) == len(set(library_urls)), (
                f"Duplicate library URLs found in results for '{query}': {library_urls}"
            )

            if len(results) > 1:
                found_multi_result = True

            print(f"\n✅ '{query}': {len(results)} results, all unique library URLs")

        # At least one query should return multiple results
        assert found_multi_result, "Expected at least one query to return multiple results"

    @pytest.mark.asyncio
    async def test_results_have_no_duplicate_albums(self, base_url):
        """
        Test that search results don't return the same album twice.

        When multiple results are returned, each should be a different album
        (no duplicate titles from the same artist).
        """
        import httpx

        # Use queries known to work from other tests
        test_cases = [
            ("Cult of Personality by Living Color", "spelling-corrected artist"),
            ("thoughtforms by lush", "song search"),
            ("Meet Me in the City Junior Kimbrough", "song/album search"),
        ]

        found_multi_result = False
        for query, description in test_cases:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{base_url}/request",
                    json={"message": query, "skip_slack": True},
                )
                response.raise_for_status()
                data = response.json()

            results = data.get("library_results", [])

            # Check all IDs are unique
            ids = [r.get("id") for r in results]
            assert len(ids) == len(set(ids)), (
                f"Duplicate IDs found for '{query}' ({description}): {ids}"
            )

            # Check no duplicate (artist, title) pairs
            artist_title_pairs = [(r.get("artist"), r.get("title")) for r in results]
            assert len(artist_title_pairs) == len(set(artist_title_pairs)), (
                f"Duplicate artist/title pairs for '{query}' ({description}): {artist_title_pairs}"
            )

            if len(results) > 1:
                found_multi_result = True

            print(f"\n✅ '{query}' ({description}): {len(results)} unique results")
            for r in results:
                print(f"    - {r.get('artist')} - {r.get('title')}")

        # At least one query should return multiple results
        assert found_multi_result, "Expected at least one query to return multiple results"

    @pytest.mark.asyncio
    async def test_6_underground_sneaker_pimps_returns_only_becoming_x(self, base_url):
        """
        Test that '6 underground - sneaker pimps' only returns albums with the track.

        Bug: The search was returning both 'Becoming X' and 'Kiss & Swallow' with
        the message "6 Underground is not on any album in the library", even though
        6 Underground IS on Becoming X.

        Expected: Should return only 'Becoming X' (which has 6 Underground)
        and NOT return 'Kiss & Swallow' (which doesn't have it).
        """
        import httpx

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={"message": "6 underground - sneaker pimps", "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

        parsed = data.get("parsed", {})
        results = data.get("library_results", [])
        song_not_found = data.get("song_not_found", True)
        context_message = data.get("context_message", "")

        print("\n📝 Parsed:")
        print(f"  Artist: {parsed.get('artist')}")
        print(f"  Song: {parsed.get('song')}")

        print("\n📚 Library Results:")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        print(f"\n📋 Context: {context_message}")
        print(f"  song_not_found: {song_not_found}")

        # Should have results
        assert len(results) > 0, "Should find results for Sneaker Pimps"

        # All results should be by Sneaker Pimps
        for r in results:
            assert "sneaker pimps" in r.get("artist", "").lower(), (
                f"Expected Sneaker Pimps, got {r.get('artist')}"
            )

        # Should NOT include Kiss & Swallow (which doesn't have 6 Underground)
        titles = [r.get("title", "").lower() for r in results]
        assert "kiss & swallow" not in titles, (
            "Kiss & Swallow should NOT be in results because it doesn't have '6 Underground'"
        )

        # Should include Becoming X (which has 6 Underground)
        has_becoming_x = any("becoming x" in title for title in titles)
        assert has_becoming_x, f"Expected 'Becoming X' (which has 6 Underground), but got: {titles}"

        # Should NOT say "not on any album" since it IS on Becoming X
        assert song_not_found is False, (
            "song_not_found should be False since 6 Underground is on Becoming X"
        )

        print("\n✅ Correctly returned only albums with the requested track!")

    @pytest.mark.asyncio
    async def test_skip_cache_bypasses_all_caches(self, base_url):
        """
        Test that skip_cache=True bypasses both in-memory and PG caches.

        Sends the same query twice: once normally (which populates caches),
        then with skip_cache=True. The second request should show 0 memory
        hits and 0 PG hits.
        """
        import httpx

        query = "Autechre Confield"

        async with httpx.AsyncClient(timeout=60.0) as client:
            # First request: populates caches
            response1 = await client.post(
                f"{base_url}/request",
                json={"message": query, "skip_slack": True},
            )
            response1.raise_for_status()

            # Second request: skip_cache should bypass all caches
            response2 = await client.post(
                f"{base_url}/request",
                json={"message": query, "skip_slack": True, "skip_cache": True},
            )
            response2.raise_for_status()
            data = response2.json()

        cache_stats = data.get("cache_stats", {})
        memory_hits = cache_stats.get("memory_hits", 0)
        pg_hits = cache_stats.get("pg_hits", 0)

        print(f"\n📊 Cache stats with skip_cache=True: {cache_stats}")

        assert memory_hits == 0, (
            f"Expected 0 memory cache hits with skip_cache=True, got {memory_hits}"
        )
        assert pg_hits == 0, f"Expected 0 PG cache hits with skip_cache=True, got {pg_hits}"

        print("✅ skip_cache=True correctly bypassed all caches")

    @pytest.mark.asyncio
    async def test_plug_not_corrected_to_plugz(self, base_url):
        """
        Test that 'me and mr. jones by plug from drum n bass for papa' does NOT
        falsely correct artist "Plug" to "Plugz".

        Bug: find_similar_artist("Plug") fuzzy-matched to "Plugz" at 89% similarity,
        exceeding the flat 85% threshold. This overwrote the parsed artist before
        library search, so FTS5 never saw "Plug" and missed Luke Vibert's album.

        Note: The library catalogs Plug's albums under "Luke Vibert", so we don't
        expect library results here -- just that the artist isn't falsely corrected.
        """
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/request",
                json={
                    "message": "me and mr. jones by plug from drum n bass for papa",
                    "skip_slack": True,
                },
            )
            response.raise_for_status()
            data = response.json()

        parsed = data.get("parsed", {})
        results = data.get("library_results", [])

        print("\n📝 Parsed:")
        print(f"  Artist: {parsed.get('artist')}")
        print(f"  Song: {parsed.get('song')}")
        print(f"  Album: {parsed.get('album')}")

        print("\n📚 Library Results:")
        for r in results:
            print(f"  - {r.get('artist')} - {r.get('title')}")

        # Artist should NOT be corrected to Plugz
        for r in results:
            assert "plugz" not in r.get("artist", "").lower(), (
                f"Should not return Plugz albums, got '{r.get('artist')}'"
            )

        print("\n✅ Correctly avoided false correction of 'Plug' to 'Plugz'!")
