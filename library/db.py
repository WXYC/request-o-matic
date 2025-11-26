import logging
from pathlib import Path
from typing import Optional

import aiosqlite

from library.models import LibraryItem

logger = logging.getLogger(__name__)

# Default path to SQLite database (relative to project root)
DEFAULT_DB_PATH = Path(__file__).parent.parent / "library.db"


class LibraryDB:
    """Async SQLite client for library catalog searches."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        """Open database connection."""
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"Library database not found at {self.db_path}. "
                "Run 'python scripts/export_to_sqlite.py' to create it."
            )

        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        logger.info(f"Connected to SQLite database: {self.db_path}")

    async def close(self):
        """Close database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("Closed SQLite connection")

    async def search(
        self,
        query: Optional[str] = None,
        artist: Optional[str] = None,
        title: Optional[str] = None,
        limit: int = 10,
        fallback_to_like: bool = True,
    ) -> list[LibraryItem]:
        """
        Search the library catalog.

        Args:
            query: Full-text search across artist and title
            artist: Filter by artist name (partial match)
            title: Filter by title (partial match)
            limit: Max results to return
            fallback_to_like: If True and FTS query returns no results, try LIKE search on individual words

        Returns:
            List of matching LibraryItems
        """
        if not self._conn:
            raise RuntimeError("Database not connected")

        if query:
            # Full-text search using FTS5
            sql = """
                SELECT l.id, l.title, l.artist, l.call_letters, l.artist_call_number, l.release_call_number, l.genre, l.format
                FROM library l
                JOIN library_fts fts ON l.id = fts.rowid
                WHERE library_fts MATCH ?
                LIMIT ?
            """
            try:
                cursor = await self._conn.execute(sql, (query, limit))
                rows = await cursor.fetchall()
                
                # If no results and fallback enabled, try LIKE search
                if not rows and fallback_to_like:
                    logger.info(f"FTS search for '{query}' returned no results, trying LIKE fallback")
                    rows = await self._fallback_like_search(query, limit)
            except Exception as e:
                # FTS syntax errors (e.g., special characters) - fall back to LIKE
                if fallback_to_like:
                    logger.info(f"FTS search for '{query}' failed ({e}), trying LIKE fallback")
                    rows = await self._fallback_like_search(query, limit)
                else:
                    raise
            
            # Return results from FTS or fallback search
            return [LibraryItem(**dict(row)) for row in rows]

        elif artist or title:
            # Filtered search
            conditions = []
            params = []
            if artist:
                conditions.append("artist LIKE ?")
                params.append(f"%{artist}%")
            if title:
                conditions.append("title LIKE ?")
                params.append(f"%{title}%")
            params.append(limit)

            sql = f"""
                SELECT id, title, artist, call_letters, artist_call_number, release_call_number, genre, format
                FROM library
                WHERE {' AND '.join(conditions)}
                LIMIT ?
            """
            cursor = await self._conn.execute(sql, params)
            rows = await cursor.fetchall()

        else:
            return []

        return [LibraryItem(**dict(row)) for row in rows]

    async def _fallback_like_search(self, query: str, limit: int) -> list[aiosqlite.Row]:
        """
        Fallback search using LIKE when FTS fails.
        Splits query into words and searches for titles/artists containing all words.
        Handles cases where punctuation or articles like "The" cause FTS to fail.
        """
        import re
        
        # Normalize: remove special chars, keep only alphanumeric and spaces
        normalized = re.sub(r'[^a-z0-9\s]', ' ', query.lower())
        words = normalized.split()
        
        # Remove common articles that might cause mismatches
        stop_words = {'the', 'a', 'an'}
        significant_words = [w for w in words if w not in stop_words and len(w) > 1]
        
        # If we removed all words, use original words
        if not significant_words:
            significant_words = [w for w in words if len(w) > 1]
        
        if not significant_words:
            return []
        
        # Build LIKE conditions for each word
        conditions = []
        params = []
        for word in significant_words:
            # Search in both title and artist fields
            conditions.append("(title LIKE ? OR artist LIKE ?)")
            params.append(f"%{word}%")
            params.append(f"%{word}%")
        
        params.append(limit)
        
        sql = f"""
            SELECT id, title, artist, call_letters, artist_call_number, release_call_number, genre, format
            FROM library
            WHERE {' AND '.join(conditions)}
            LIMIT ?
        """
        
        cursor = await self._conn.execute(sql, params)
        return await cursor.fetchall()
