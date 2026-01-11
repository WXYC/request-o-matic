import pytest
import pytest_asyncio
from pathlib import Path
import tempfile
import aiosqlite

from library.db import LibraryDB
from library.models import LibraryItem


# --- Fixtures ---


@pytest_asyncio.fixture
async def test_db(tmp_path):
    """Create a temporary test database with sample data."""
    # Create a temporary database file
    db_path = tmp_path / "test_library.db"
    
    # Initialize database with schema
    async with aiosqlite.connect(db_path) as conn:
        # Create main table
        await conn.execute("""
            CREATE TABLE library (
                id INTEGER PRIMARY KEY,
                title TEXT,
                artist TEXT,
                call_letters TEXT,
                artist_call_number INTEGER,
                release_call_number INTEGER,
                genre TEXT,
                format TEXT
            )
        """)
        
        # Create FTS5 virtual table
        await conn.execute("""
            CREATE VIRTUAL TABLE library_fts USING fts5(
                title,
                artist,
                content='library',
                content_rowid='id'
            )
        """)
        
        # Insert test data
        test_albums = [
            (1, "Richard D. James Album", "Aphex Twin", "Ap", 2, 8, "Electronic", "cd"),
            (2, "Selected Ambient works vol 2", "Aphex Twin", "Ap", 2, 1, "Electronic", "vinyl"),
            (3, "Morvern Callar", "Soundtracks - M", "M", 1, 1, "Soundtrack", "cd"),
            (4, "The Stone Roses", "The Stone Roses", "St", 1, 1, "Rock", "vinyl"),
            (5, "Second Coming", "The Stone Roses", "St", 1, 2, "Rock", "cd"),
            (6, "OK Computer", "Radiohead", "Ra", 1, 3, "Rock", "cd"),
            (7, "A Love Supreme", "John Coltrane", "Co", 5, 1, "Jazz", "vinyl"),
        ]
        
        await conn.executemany(
            "INSERT INTO library VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            test_albums
        )
        
        # Populate FTS table
        await conn.execute("""
            INSERT INTO library_fts(rowid, title, artist)
            SELECT id, title, artist FROM library
        """)
        
        await conn.commit()
    
    # Create LibraryDB instance
    db = LibraryDB(db_path=db_path)
    await db.connect()
    
    yield db
    
    # Cleanup
    await db.close()
    db_path.unlink()


# --- Normal FTS Search Tests ---


class TestFTSSearch:
    @pytest.mark.asyncio
    async def test_search_by_artist(self, test_db: LibraryDB):
        """Test basic FTS search by artist name."""
        results = await test_db.search(query="Aphex Twin", limit=10)
        
        assert len(results) == 2
        assert all(r.artist == "Aphex Twin" for r in results)
        titles = {r.title for r in results}
        assert "Richard D. James Album" in titles
        assert "Selected Ambient works vol 2" in titles
    
    @pytest.mark.asyncio
    async def test_search_by_album_title(self, test_db: LibraryDB):
        """Test FTS search by album title."""
        results = await test_db.search(query="OK Computer", limit=10)
        
        assert len(results) == 1
        assert results[0].title == "OK Computer"
        assert results[0].artist == "Radiohead"
    
    @pytest.mark.asyncio
    async def test_search_by_artist_and_album(self, test_db: LibraryDB):
        """Test FTS search with both artist and album."""
        results = await test_db.search(query="Aphex Twin Richard D James Album", limit=10)
        
        assert len(results) >= 1
        assert results[0].title == "Richard D. James Album"
        assert results[0].artist == "Aphex Twin"
    
    @pytest.mark.asyncio
    async def test_search_no_results(self, test_db: LibraryDB):
        """Test FTS search with no matching results."""
        results = await test_db.search(query="Nonexistent Band Album", limit=10)
        
        assert len(results) == 0
    
    @pytest.mark.asyncio
    async def test_search_limit(self, test_db: LibraryDB):
        """Test that limit parameter is respected."""
        results = await test_db.search(query="The", limit=2)
        
        assert len(results) <= 2


# --- Fallback LIKE Search Tests ---


class TestFallbackSearch:
    @pytest.mark.asyncio
    async def test_fallback_with_special_characters(self, test_db: LibraryDB):
        """Test that search with special characters triggers fallback."""
        # This query would cause FTS syntax error due to periods
        results = await test_db.search(
            query="Aphex Twin Richard D. James Album", 
            limit=10
        )
        
        # Should find the album via fallback search
        assert len(results) >= 1
        found_titles = {r.title for r in results}
        assert "Richard D. James Album" in found_titles
    
    @pytest.mark.asyncio
    async def test_fallback_removes_articles(self, test_db: LibraryDB):
        """Test that fallback search removes common articles."""
        # Search with "The" which FTS might handle differently
        results = await test_db.search(
            query="The Stone Roses The Stone Roses",
            limit=10
        )
        
        assert len(results) >= 1
        found = any(r.title == "The Stone Roses" for r in results)
        assert found
    
    @pytest.mark.asyncio
    async def test_fallback_with_punctuation(self, test_db: LibraryDB):
        """Test fallback with various punctuation marks."""
        # Japanese characters and equals sign would trigger fallback
        results = await test_db.search(
            query="Richard D. James Album = リチャード・D. ジェイムス・アルブム",
            limit=10
        )
        
        assert len(results) >= 1
        assert any(r.title == "Richard D. James Album" for r in results)
    
    @pytest.mark.asyncio
    async def test_fallback_partial_word_match(self, test_db: LibraryDB):
        """Test that fallback finds partial word matches."""
        results = await test_db.search(query="Ambient works", limit=10)
        
        assert len(results) >= 1
        assert any("Ambient" in r.title for r in results)
    
    @pytest.mark.asyncio
    async def test_fallback_case_insensitive(self, test_db: LibraryDB):
        """Test that fallback search is case insensitive."""
        results = await test_db.search(query="APHEX TWIN", limit=10)
        
        assert len(results) >= 1
        assert any(r.artist == "Aphex Twin" for r in results)
    
    @pytest.mark.asyncio
    async def test_fallback_disabled(self, test_db: LibraryDB):
        """Test that fallback can be disabled."""
        # Query with special chars that would normally trigger fallback
        with pytest.raises(Exception):
            await test_db.search(
                query="Richard D. James Album = Test",
                limit=10,
                fallback_to_like=False
            )


# --- Filtered Search Tests ---


class TestFilteredSearch:
    @pytest.mark.asyncio
    async def test_search_by_artist_filter(self, test_db: LibraryDB):
        """Test filtered search by artist."""
        results = await test_db.search(artist="Aphex Twin", limit=10)
        
        assert len(results) == 2
        assert all(r.artist == "Aphex Twin" for r in results)
    
    @pytest.mark.asyncio
    async def test_search_by_title_filter(self, test_db: LibraryDB):
        """Test filtered search by title."""
        results = await test_db.search(title="Stone Roses", limit=10)
        
        assert len(results) >= 1
        assert any("Stone Roses" in r.title for r in results)
    
    @pytest.mark.asyncio
    async def test_search_by_artist_and_title_filter(self, test_db: LibraryDB):
        """Test filtered search with both artist and title."""
        results = await test_db.search(
            artist="Stone Roses",
            title="Second Coming",
            limit=10
        )
        
        assert len(results) == 1
        assert results[0].title == "Second Coming"
        assert results[0].artist == "The Stone Roses"
    
    @pytest.mark.asyncio
    async def test_search_filter_partial_match(self, test_db: LibraryDB):
        """Test that filters use partial (LIKE) matching."""
        results = await test_db.search(artist="Radiohe", limit=10)
        
        assert len(results) == 1
        assert results[0].artist == "Radiohead"


# --- Edge Cases and Error Handling ---


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_query(self, test_db: LibraryDB):
        """Test that empty query returns no results."""
        results = await test_db.search(query="", limit=10)
        
        assert len(results) == 0
    
    @pytest.mark.asyncio
    async def test_none_query(self, test_db: LibraryDB):
        """Test that None query with no filters returns empty."""
        results = await test_db.search(query=None, limit=10)
        
        assert len(results) == 0
    
    @pytest.mark.asyncio
    async def test_query_only_special_chars(self, test_db: LibraryDB):
        """Test query with only special characters."""
        results = await test_db.search(query="=== ... !!!!", limit=10)
        
        # Should return empty since no meaningful words
        assert len(results) == 0
    
    @pytest.mark.asyncio
    async def test_query_only_stop_words(self, test_db: LibraryDB):
        """Test query with only stop words."""
        results = await test_db.search(query="the a an", limit=10)
        
        # Might return some results but should handle gracefully
        assert isinstance(results, list)
    
    @pytest.mark.asyncio
    async def test_very_long_query(self, test_db: LibraryDB):
        """Test search with very long query string."""
        long_query = " ".join(["word"] * 100)
        results = await test_db.search(query=long_query, limit=10)
        
        # Should handle without crashing
        assert isinstance(results, list)
    
    @pytest.mark.asyncio
    async def test_unicode_characters(self, test_db: LibraryDB):
        """Test search with various unicode characters."""
        results = await test_db.search(
            query="Aphex Twin リチャード 中文 العربية",
            limit=10
        )
        
        # Should filter out unicode and find Aphex Twin
        assert len(results) >= 1


# --- Library Item Model Tests ---


class TestLibraryItem:
    @pytest.mark.asyncio
    async def test_library_item_fields(self, test_db: LibraryDB):
        """Test that LibraryItem has all expected fields."""
        results = await test_db.search(query="Aphex Twin", limit=1)
        
        assert len(results) == 1
        item = results[0]
        
        assert hasattr(item, 'id')
        assert hasattr(item, 'title')
        assert hasattr(item, 'artist')
        assert hasattr(item, 'call_letters')
        assert hasattr(item, 'artist_call_number')
        assert hasattr(item, 'release_call_number')
        assert hasattr(item, 'genre')
        assert hasattr(item, 'format')
    
    @pytest.mark.asyncio
    async def test_library_item_call_number(self, test_db: LibraryDB):
        """Test that call_number computed field works."""
        results = await test_db.search(query="Aphex Twin", limit=1)
        
        assert len(results) == 1
        item = results[0]
        
        # Should have call_number property
        assert hasattr(item, 'call_number')
        expected = f"{item.genre} {item.format} {item.call_letters} {item.artist_call_number}/{item.release_call_number}"
        assert item.call_number == expected


# --- Real-World Scenarios ---


class TestRealWorldScenarios:
    @pytest.mark.asyncio
    async def test_goon_gumpas_scenario(self, test_db: LibraryDB):
        """Test the actual Goon Gumpas / Richard D. James Album scenario."""
        # Simulate what Discogs returns
        discogs_album_name = "Richard D. James Album = リチャード・D. ジェイムス・アルブム"
        
        results = await test_db.search(
            query=f"Aphex Twin {discogs_album_name}",
            limit=5
        )
        
        # Should find the album via fallback
        assert len(results) >= 1
        assert any(r.title == "Richard D. James Album" for r in results)
    
    @pytest.mark.asyncio
    async def test_morvern_callar_soundtrack(self, test_db: LibraryDB):
        """Test finding Morvern Callar soundtrack."""
        results = await test_db.search(query="Morvern Callar", limit=5)
        
        assert len(results) == 1
        assert results[0].title == "Morvern Callar"
        assert results[0].genre == "Soundtrack"
    
    @pytest.mark.asyncio
    async def test_various_artists_compilation(self, test_db: LibraryDB):
        """Test finding albums with 'Various' or similar artists."""
        results = await test_db.search(query="Soundtracks", limit=5)

        # Should find soundtracks section
        assert len(results) >= 1


# --- Artist Spelling Correction Tests ---


@pytest_asyncio.fixture
async def spelling_db(tmp_path):
    """Create a test database with artists that have spelling variants."""
    db_path = tmp_path / "spelling_test.db"

    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("""
            CREATE TABLE library (
                id INTEGER PRIMARY KEY,
                title TEXT,
                artist TEXT,
                call_letters TEXT,
                artist_call_number INTEGER,
                release_call_number INTEGER,
                genre TEXT,
                format TEXT
            )
        """)

        await conn.execute("""
            CREATE VIRTUAL TABLE library_fts USING fts5(
                title, artist, content='library', content_rowid='id'
            )
        """)

        # Artists with common spelling variants
        test_albums = [
            (1, "Vivid", "Living Colour", "LI", 10, 1, "Rock", "cd"),
            (2, "Time's Up", "Living Colour", "LI", 10, 2, "Rock", "vinyl"),
            (3, "Theatre of Pain", "Mötley Crüe", "MO", 5, 1, "Rock", "vinyl"),
            (4, "Favourite Worst Nightmare", "Arctic Monkeys", "AR", 3, 2, "Rock", "cd"),
            (5, "Led Zeppelin IV", "Led Zeppelin", "LE", 1, 4, "Rock", "vinyl"),
        ]

        await conn.executemany(
            "INSERT INTO library VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            test_albums
        )

        await conn.execute("""
            INSERT INTO library_fts(rowid, title, artist)
            SELECT id, title, artist FROM library
        """)

        await conn.commit()

    db = LibraryDB(db_path=db_path)
    await db.connect()
    yield db
    await db.close()


class TestFindSimilarArtist:
    @pytest.mark.asyncio
    async def test_color_to_colour(self, spelling_db: LibraryDB):
        """Test that 'Living Color' corrects to 'Living Colour'."""
        result = await spelling_db.find_similar_artist("Living Color")

        assert result == "Living Colour"

    @pytest.mark.asyncio
    async def test_exact_match_returns_none(self, spelling_db: LibraryDB):
        """Test that exact match returns None (no correction needed)."""
        result = await spelling_db.find_similar_artist("Living Colour")

        assert result is None

    @pytest.mark.asyncio
    async def test_case_insensitive_match(self, spelling_db: LibraryDB):
        """Test that matching is case-insensitive."""
        result = await spelling_db.find_similar_artist("living color")

        assert result == "Living Colour"

    @pytest.mark.asyncio
    async def test_no_match_returns_none(self, spelling_db: LibraryDB):
        """Test that non-matching artist returns None."""
        result = await spelling_db.find_similar_artist("Nonexistent Band")

        assert result is None

    @pytest.mark.asyncio
    async def test_threshold_respected(self, spelling_db: LibraryDB):
        """Test that low-scoring matches are rejected."""
        # "Led Zepplin" (typo) should match "Led Zeppelin"
        result = await spelling_db.find_similar_artist("Led Zepplin")
        assert result == "Led Zeppelin"

        # "Led" alone should not match (too different)
        result = await spelling_db.find_similar_artist("Led")
        assert result is None

    @pytest.mark.asyncio
    async def test_prefix_search_uses_first_word(self, spelling_db: LibraryDB):
        """Test that prefix search uses first significant word."""
        # "Arctic Monkies" (typo) should find "Arctic Monkeys"
        result = await spelling_db.find_similar_artist("Arctic Monkies")

        assert result == "Arctic Monkeys"

