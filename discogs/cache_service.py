"""PostgreSQL cache service for Discogs data.

This service provides a local cache of Discogs release data stored in PostgreSQL.
It implements a hybrid cache strategy:
1. Query local DB first
2. On cache miss, caller should query Discogs API
3. Cache API results back to local DB for future queries

The cache uses PostgreSQL's pg_trgm extension for fuzzy text matching.
"""

import logging

from core.matching import validate_track_on_tracklist
from discogs.models import ReleaseInfo, ReleaseMetadataResponse, TrackItem

logger = logging.getLogger(__name__)


class CacheUnavailableError(Exception):
    """Raised when the PostgreSQL cache is unreachable."""

    pass


class DiscogsCacheService:
    """Service for querying and updating the local Discogs cache.

    This service wraps a PostgreSQL connection pool and provides methods
    for searching and retrieving cached Discogs data.
    """

    def __init__(self, pool):
        """Initialize the cache service with a connection pool.

        Args:
            pool: asyncpg connection pool
        """
        self.pool = pool

    async def is_available(self) -> bool:
        """Check if the cache database is available.

        Returns:
            True if database responds to health check, False otherwise
        """
        try:
            result = await self.pool.fetchval("SELECT 1")
            return bool(result == 1)
        except Exception as e:
            logger.warning(f"Cache health check failed: {e}")
            return False

    async def search_releases_by_track(
        self, track: str, artist: str | None = None, limit: int = 20
    ) -> list[ReleaseInfo]:
        """Search for releases containing a track.

        Uses trigram similarity for fuzzy matching on track title.

        Args:
            track: Track title to search for
            artist: Optional artist name to filter by
            limit: Maximum number of results to return

        Returns:
            List of ReleaseInfo objects

        Raises:
            CacheUnavailableError: If database is unreachable
        """
        try:
            # Use trigram similarity for fuzzy matching
            # Filter first on tracks, then join for release details
            query = """
                WITH matching_tracks AS (
                    SELECT DISTINCT rt.release_id, rt.title as track_title,
                           similarity(lower(rt.title), lower($1)) as sim
                    FROM release_track rt
                    WHERE lower(rt.title) % lower($1)
                    ORDER BY sim DESC
                    LIMIT $2
                )
                SELECT r.id as release_id, r.title, ra.artist_name,
                       mt.track_title,
                       CASE WHEN lower(ra.artist_name) LIKE '%various%' THEN true ELSE false END as is_compilation
                FROM matching_tracks mt
                JOIN release r ON r.id = mt.release_id
                JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
                WHERE ($3::text IS NULL OR lower(ra.artist_name) % lower($3))
                ORDER BY mt.sim DESC
            """

            rows = await self.pool.fetch(query, track, limit * 2, artist)

            results = []
            seen_albums = set()

            for row in rows:
                album = row["title"]
                album_key = album.lower()

                if album_key in seen_albums:
                    continue
                seen_albums.add(album_key)

                results.append(
                    ReleaseInfo(
                        album=album,
                        artist=row["artist_name"],
                        release_id=row["release_id"],
                        is_compilation=row["is_compilation"],
                    )
                )

                if len(results) >= limit:
                    break

            return results

        except Exception as e:
            logger.error(f"Cache search failed: {e}")
            raise CacheUnavailableError(f"Cache search failed: {e}") from e

    async def get_release(self, release_id: int) -> ReleaseMetadataResponse | None:
        """Get full release metadata by ID.

        Args:
            release_id: Discogs release ID

        Returns:
            ReleaseMetadataResponse if found, None if not in cache

        Raises:
            CacheUnavailableError: If database is unreachable
        """
        try:
            # Get release basic info
            release_row = await self.pool.fetchrow(
                "SELECT id, title, release_year, artwork_url FROM release WHERE id = $1",
                release_id,
            )

            if release_row is None:
                return None

            # Get artists
            artist_rows = await self.pool.fetch(
                "SELECT artist_name, extra FROM release_artist WHERE release_id = $1 ORDER BY extra",
                release_id,
            )

            # Get primary artist (extra = 0)
            primary_artist = ""
            for row in artist_rows:
                if row["extra"] == 0:
                    primary_artist = row["artist_name"]
                    break

            # Get tracks
            track_rows = await self.pool.fetch(
                """
                SELECT position, title, duration, sequence
                FROM release_track
                WHERE release_id = $1
                ORDER BY sequence
                """,
                release_id,
            )

            # Get per-track artists (for compilations)
            track_artist_rows = await self.pool.fetch(
                """
                SELECT track_sequence, artist_name
                FROM release_track_artist
                WHERE release_id = $1
                ORDER BY track_sequence
                """,
                release_id,
            )

            # Build track artist mapping
            track_artists: dict[int, list[str]] = {}
            for row in track_artist_rows:
                seq = row["track_sequence"]
                if seq not in track_artists:
                    track_artists[seq] = []
                track_artists[seq].append(row["artist_name"])

            # Build tracklist
            tracklist = []
            for row in track_rows:
                seq = row["sequence"]
                tracklist.append(
                    TrackItem(
                        position=row["position"] or "",
                        title=row["title"],
                        duration=row["duration"],
                        artists=track_artists.get(seq, []),
                    )
                )

            return ReleaseMetadataResponse(
                release_id=release_id,
                title=release_row["title"],
                artist=primary_artist,
                year=release_row["release_year"],
                artwork_url=release_row["artwork_url"],
                tracklist=tracklist,
                cached=True,
            )

        except Exception as e:
            logger.error(f"Cache get_release failed: {e}")
            raise CacheUnavailableError(f"Cache get_release failed: {e}") from e

    async def write_release(self, release: ReleaseMetadataResponse) -> None:
        """Write or update a release in the cache.

        Used to cache API responses for future queries.

        Args:
            release: Release metadata to cache

        Raises:
            CacheUnavailableError: If database is unreachable
        """
        try:
            async with self.pool.acquire() as conn:
                # Upsert release
                await conn.execute(
                    """
                    INSERT INTO release (id, title, release_year, artwork_url)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (id) DO UPDATE SET
                        title = EXCLUDED.title,
                        release_year = EXCLUDED.release_year,
                        artwork_url = EXCLUDED.artwork_url
                    """,
                    release.release_id,
                    release.title,
                    release.year,
                    release.artwork_url,
                )

                # Upsert primary artist
                if release.artist:
                    await conn.execute(
                        """
                        INSERT INTO release_artist (release_id, artist_name, extra)
                        VALUES ($1, $2, 0)
                        ON CONFLICT (release_id, artist_name) DO NOTHING
                        """,
                        release.release_id,
                        release.artist,
                    )

                # Delete existing tracks (simpler than upsert for lists)
                await conn.execute(
                    "DELETE FROM release_track WHERE release_id = $1",
                    release.release_id,
                )

                # Insert tracks
                if release.tracklist:
                    track_data = [
                        (release.release_id, i + 1, t.position, t.title, t.duration)
                        for i, t in enumerate(release.tracklist)
                    ]
                    await conn.executemany(
                        """
                        INSERT INTO release_track (release_id, sequence, position, title, duration)
                        VALUES ($1, $2, $3, $4, $5)
                        """,
                        track_data,
                    )

                    # Insert per-track artists
                    track_artist_data = []
                    for i, t in enumerate(release.tracklist):
                        for artist in t.artists:
                            track_artist_data.append((release.release_id, i + 1, artist))

                    if track_artist_data:
                        await conn.executemany(
                            """
                            INSERT INTO release_track_artist (release_id, track_sequence, artist_name)
                            VALUES ($1, $2, $3)
                            ON CONFLICT DO NOTHING
                            """,
                            track_artist_data,
                        )

                # Update cache metadata
                await conn.execute(
                    """
                    INSERT INTO cache_metadata (release_id, source)
                    VALUES ($1, 'api_fetch')
                    ON CONFLICT (release_id) DO UPDATE SET
                        cached_at = now(),
                        source = 'api_fetch'
                    """,
                    release.release_id,
                )

                logger.debug(f"Cached release {release.release_id}: {release.title}")

        except Exception as e:
            logger.error(f"Cache write_release failed: {e}")
            raise CacheUnavailableError(f"Cache write_release failed: {e}") from e

    def _build_search_query(
        self,
        artist: str | None = None,
        album: str | None = None,
        limit: int = 5,
    ) -> tuple[str, list]:
        """Build a parameterized SQL query for release search.

        Generates the appropriate WHERE clause, similarity scoring, and positional
        parameters based on which search fields are provided. This replaces three
        near-identical query branches with a single parameterized builder.

        Args:
            artist: Artist name to search for
            album: Album/release title to search for
            limit: Maximum number of results to return

        Returns:
            Tuple of (query_sql, params) ready for pool.fetch()
        """
        conditions = []
        similarity_cols = []
        params: list = []
        idx = 1

        if album:
            conditions.append(f"lower(r.title) % lower(${idx})")
            similarity_cols.append(f"similarity(lower(r.title), lower(${idx}))")
            params.append(album)
            idx += 1
        if artist:
            conditions.append(f"lower(ra.artist_name) % lower(${idx})")
            similarity_cols.append(f"similarity(lower(ra.artist_name), lower(${idx}))")
            params.append(artist)
            idx += 1

        score_expr = (
            f"GREATEST({', '.join(similarity_cols)})"
            if len(similarity_cols) > 1
            else similarity_cols[0]
        )

        inner = f"""
            SELECT DISTINCT ON (r.id)
                r.id as release_id, r.title, ra.artist_name, r.artwork_url,
                {score_expr} as score
            FROM release r
            JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
            WHERE {" OR ".join(conditions)}
            ORDER BY r.id, score DESC
        """

        params.append(limit * 2)
        query = f"""
            SELECT * FROM ({inner}) sub
            ORDER BY score DESC
            LIMIT ${idx}
        """

        return query, params

    async def search_releases(
        self, artist: str | None = None, album: str | None = None, limit: int = 5
    ) -> list[dict]:
        """Search for releases by artist and/or album title.

        Uses trigram similarity for fuzzy matching on release title and artist name.

        Args:
            artist: Artist name to search for
            album: Album/release title to search for
            limit: Maximum number of results to return

        Returns:
            List of dicts with keys: release_id, title, artist_name, artwork_url

        Raises:
            CacheUnavailableError: If database is unreachable
        """
        if not artist and not album:
            return []

        try:
            query, params = self._build_search_query(artist=artist, album=album, limit=limit)
            rows = await self.pool.fetch(query, *params)

            results = []
            seen_titles = set()
            for row in rows:
                title_key = row["title"].lower()
                if title_key in seen_titles:
                    continue
                seen_titles.add(title_key)

                results.append(
                    {
                        "release_id": row["release_id"],
                        "title": row["title"],
                        "artist_name": row["artist_name"],
                        "artwork_url": row["artwork_url"],
                    }
                )

                if len(results) >= limit:
                    break

            return results

        except Exception as e:
            logger.error(f"Cache search_releases failed: {e}")
            raise CacheUnavailableError(f"Cache search_releases failed: {e}") from e

    async def validate_track_on_release(
        self, release_id: int, track: str, artist: str
    ) -> bool | None:
        """Validate that a track by an artist exists on a release.

        Args:
            release_id: Discogs release ID
            track: Track title to find
            artist: Artist name to find

        Returns:
            True if track by artist found, False if not found, None if release not cached

        Raises:
            CacheUnavailableError: If database is unreachable
        """
        # First get the full release to check if it's in cache
        release = await self.get_release(release_id)
        if release is None:
            return None  # Cache miss - caller should try API

        return validate_track_on_tracklist(release.tracklist, release.artist, track, artist)
