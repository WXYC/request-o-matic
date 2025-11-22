import logging
from typing import Optional

import aiomysql

from library.models import LibraryItem

logger = logging.getLogger(__name__)


class LibraryDB:
    """Async MySQL client for library catalog searches."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 3306,
        user: str = "root",
        password: str = "",
        database: str = "wxyc_library",
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self._pool: Optional[aiomysql.Pool] = None

    async def connect(self):
        """Create connection pool."""
        self._pool = await aiomysql.create_pool(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            db=self.database,
            autocommit=True,
            minsize=1,
            maxsize=10,
        )
        logger.info(f"Connected to MySQL database: {self.database}")

    async def close(self):
        """Close connection pool."""
        if self._pool:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None
            logger.info("Closed MySQL connection pool")

    async def search(
        self,
        query: Optional[str] = None,
        artist: Optional[str] = None,
        title: Optional[str] = None,
        limit: int = 10,
    ) -> list[LibraryItem]:
        """
        Search the library catalog.

        Args:
            query: Full-text search across artist and title
            artist: Filter by artist name (partial match)
            title: Filter by title (partial match)
            limit: Max results to return

        Returns:
            List of matching LibraryItems
        """
        if not self._pool:
            raise RuntimeError("Database not connected")

        async with self._pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                if query:
                    # Full-text search
                    sql = """
                        SELECT id, title, artist, call_letters, call_numbers, genre, format
                        FROM LIBRARY_SEARCH
                        WHERE MATCH(title, artist) AGAINST(%s IN NATURAL LANGUAGE MODE)
                        LIMIT %s
                    """
                    await cursor.execute(sql, (query, limit))
                elif artist or title:
                    # Filtered search
                    conditions = []
                    params = []
                    if artist:
                        conditions.append("artist LIKE %s")
                        params.append(f"%{artist}%")
                    if title:
                        conditions.append("title LIKE %s")
                        params.append(f"%{title}%")
                    params.append(limit)

                    sql = f"""
                        SELECT id, title, artist, call_letters, call_numbers, genre, format
                        FROM LIBRARY_SEARCH
                        WHERE {' AND '.join(conditions)}
                        LIMIT %s
                    """
                    await cursor.execute(sql, params)
                else:
                    return []

                rows = await cursor.fetchall()
                return [LibraryItem(**row) for row in rows]
