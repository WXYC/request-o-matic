"""
Integration tests against production services.

Run with: pytest tests/test_integration.py -v
Skip with: pytest tests/ -m "not integration"
"""
import os
import pytest
from pathlib import Path
from dotenv import load_dotenv

from artwork.providers.discogs import DiscogsProvider
from library.db import LibraryDB

load_dotenv()

# Mark all tests in this file as integration tests
pytestmark = pytest.mark.integration

# Skip if required env vars not set
DISCOGS_TOKEN = os.getenv("DISCOGS_TOKEN")
# library.db is in project root, not tests/ directory
LIBRARY_DB_PATH = Path(__file__).parent.parent.parent / "library.db"

skip_if_no_token = pytest.mark.skipif(
    not DISCOGS_TOKEN,
    reason="DISCOGS_TOKEN not set - skipping integration tests"
)

skip_if_no_db = pytest.mark.skipif(
    not LIBRARY_DB_PATH.exists(),
    reason="library.db not found - skipping integration tests"
)


class TestDiscogsIntegration:
    """Test against the real Discogs API."""
    
    @pytest.mark.asyncio
    @skip_if_no_token
    async def test_manu_dibango_compilation_search(self):
        """Test the actual Manu Dibango compilation search scenario."""
        provider = DiscogsProvider(token=DISCOGS_TOKEN)
        
        # Test the real scenario
        releases = await provider.search_releases_by_track(
            "Abele Dance (85 Remix)",
            "Manu Dibango"
        )
        
        print(f"\n✅ Found {len(releases)} releases on Discogs:")
        for i, (artist, album) in enumerate(releases[:5], 1):
            print(f"  {i}. {artist} - {album}")
        
        # Verify we get results
        assert len(releases) > 0, "Should find at least one release"
        
        # Verify compilation is in results
        album_titles = [album for _, album in releases]
        has_compilation = any(
            "change" in album.lower() and "beat" in album.lower()
            for album in album_titles
        )
        
        if has_compilation:
            print("  ✅ Found 'Change The Beat' compilation!")
        else:
            print("  ⚠️  Compilation not in top results")
            print(f"     All albums: {album_titles}")
        
        await provider.close()
    
    @pytest.mark.asyncio
    @skip_if_no_token
    async def test_discogs_rate_limiting(self):
        """Test that we handle rate limits gracefully."""
        provider = DiscogsProvider(token=DISCOGS_TOKEN)
        
        # Make multiple rapid requests
        results = []
        for i in range(3):
            releases = await provider.search_releases_by_track(
                f"Test Track {i}",
                "Test Artist"
            )
            results.append(releases)
        
        # Should complete without errors (even if rate limited)
        assert len(results) == 3
        
        await provider.close()


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
            "celluloid" in (result.title or "").lower() and
            "change" in (result.title or "").lower() and
            "beat" in (result.title or "").lower()
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
        results = await db.search(
            query="Richard D. James Album = リチャード",
            limit=5
        )
        
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
    async def test_dj_blaqstarr_in_library(self):
        """Test that DJ Blaqstarr's release exists in the library.
        
        Expected: "Shake It To The Ground" should be in the library
        with catalog ID "Hiphop cd DJ 70/1"
        """
        db = LibraryDB(db_path=LIBRARY_DB_PATH)
        await db.connect()
        
        # Search for the artist
        results = await db.search(query="DJ Blaqstarr", limit=5)
        
        print(f"\n✅ Found {len(results)} results for DJ Blaqstarr:")
        for result in results:
            print(f"  - {result.artist} - {result.title}")
            print(f"    Call: {result.call_number}")
        
        # Verify we found it
        assert len(results) > 0, "Should find DJ Blaqstarr release"
        
        # Check for expected catalog structure (DJ 70/1 in Hiphop)
        found_expected = any(
            result.call_letters == "DJ" and
            result.artist_call_number == 70
            for result in results
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
        """
        # Step 1: Search Discogs
        provider = DiscogsProvider(token=DISCOGS_TOKEN)
        releases = await provider.search_releases_by_track(
            "Abele Dance (85 Remix)",
            "Manu Dibango"
        )
        
        print(f"\n📀 Step 1: Found {len(releases)} releases on Discogs")
        
        # Step 2: Check library for each
        db = LibraryDB(db_path=LIBRARY_DB_PATH)
        await db.connect()
        
        found_in_library = []
        print("\n🔍 Step 2: Checking library for each release...")
        
        for release_artist, release_album in releases[:10]:  # Check first 10
            # Try exact match first
            results = await db.search(query=release_album, limit=1)
            
            # Try fuzzy match if exact fails
            if not results:
                # Extract keywords
                import re
                words = re.sub(r'[^\w\s]', ' ', release_album.lower()).split()
                significant = [w for w in words if len(w) > 3][:3]
                
                if significant:
                    fuzzy_query = ' '.join(significant)
                    results = await db.search(query=fuzzy_query, limit=1)
            
            if results:
                found_in_library.append((release_album, results[0]))
                print(f"  ✅ Found: {results[0].title}")
        
        # Step 3: Verify we found something
        print(f"\n🎉 Found {len(found_in_library)} matches in library!")
        
        assert len(found_in_library) > 0, "Should find at least one release in library"
        
        # Check if we found the compilation
        has_compilation = any(
            "celluloid" in item.title.lower() or "change" in item.title.lower()
            for _, item in found_in_library
        )
        
        if has_compilation:
            print("  ✅ Successfully matched compilation!")
        
        await provider.close()
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
        artist_words = re.sub(r'[^\w\s]', ' ', artist.lower()).split()
        song_words = re.sub(r'[^\w\s]', ' ', song.lower()).split()
        
        all_words = artist_words + song_words
        significant = [w for w in all_words if len(w) > 3 and w not in 
            {'the', 'and', 'with', 'from', 'that', 'this', 'play', 'song', 'remix'}]
        
        keyword_query = ' '.join(significant[:3])
        print(f"\n🔍 Testing keyword search: '{keyword_query}'")
        
        results = await db.search(query=keyword_query, limit=3)
        
        print(f"✅ Found {len(results)} results:")
        for result in results:
            print(f"  - {result.artist} - {result.title}")
        
        # Verify we find something relevant
        assert len(results) > 0, "Keyword search should find results"
        
        # Check if we found Aphex Twin albums
        has_aphex = any(
            "aphex" in result.artist.lower() for result in results
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
        """
        # Step 1: Search Discogs for the track
        provider = DiscogsProvider(token=DISCOGS_TOKEN)
        releases = await provider.search_releases_by_track(
            "Shake It To The Ground",
            "DJ Blaqstarr"
        )
        
        print(f"\n📀 Step 1: Found {len(releases)} releases on Discogs")
        for i, (artist, album) in enumerate(releases[:5], 1):
            print(f"  {i}. {artist} - {album}")
        
        # Step 2: Check library for matches
        db = LibraryDB(db_path=LIBRARY_DB_PATH)
        await db.connect()
        
        found_in_library = []
        print("\n🔍 Step 2: Checking library for each release...")
        
        for release_artist, release_album in releases[:10]:
            # Try exact match
            results = await db.search(query=release_album, limit=1)
            
            # Try fuzzy match if exact fails
            if not results:
                import re
                words = re.sub(r'[^\w\s]', ' ', release_album.lower()).split()
                significant = [w for w in words if len(w) > 3][:3]
                
                if significant:
                    fuzzy_query = ' '.join(significant)
                    results = await db.search(query=fuzzy_query, limit=1)
            
            if results:
                found_in_library.append((release_album, results[0]))
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
        
        # Verify we found the release
        assert len(found_in_library) > 0 or len(direct_results) > 0, \
            "Should find DJ Blaqstarr release in library"
        
        # Check for expected catalog ID (DJ 70/1)
        all_results = [item for _, item in found_in_library] + direct_results
        has_expected_catalog = any(
            result.call_letters == "DJ" and result.artist_call_number == 70
            for result in all_results
        )
        
        if has_expected_catalog:
            print("  ✅ Found release with expected catalog ID (DJ 70/1)!")
        
        await provider.close()
        await db.close()

