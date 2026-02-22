"""Tests for DiscogsCacheService (PostgreSQL cache)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from discogs.models import ReleaseInfo, ReleaseMetadataResponse, TrackItem


class TestSearchReleasesByTrack:
    """Tests for search_releases_by_track method."""

    @pytest.mark.asyncio
    async def test_returns_releases_list(self):
        """Test returns list of releases containing track with trigram search."""
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 28138,
                    "title": "Confield",
                    "artist_name": "Autechre",
                    "track_title": "VI Scose Poise",
                    "is_compilation": False,
                },
                {
                    "release_id": 99999,
                    "title": "Electronic Compilation",
                    "artist_name": "Various Artists",
                    "track_title": "VI Scose Poise",
                    "is_compilation": True,
                },
            ]
        )

        from discogs.cache_service import DiscogsCacheService

        cache = DiscogsCacheService(pool=mock_pool)
        results = await cache.search_releases_by_track(track="VI Scose Poise", artist="Autechre")

        assert len(results) == 2
        assert isinstance(results[0], ReleaseInfo)
        assert results[0].album == "Confield"
        assert results[0].artist == "Autechre"
        assert results[0].release_id == 28138
        assert results[1].is_compilation is True

    @pytest.mark.asyncio
    async def test_empty_results_on_no_match(self):
        """Test returns empty list when no matching releases found."""
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[])

        from discogs.cache_service import DiscogsCacheService

        cache = DiscogsCacheService(pool=mock_pool)
        results = await cache.search_releases_by_track(track="Nonexistent Track", artist="Unknown")

        assert results == []

    @pytest.mark.asyncio
    async def test_uses_trigram_similarity(self):
        """Test that query uses trigram similarity for fuzzy matching."""
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[])

        from discogs.cache_service import DiscogsCacheService

        cache = DiscogsCacheService(pool=mock_pool)
        await cache.search_releases_by_track(track="Blue Monday", artist="New Order")

        # Verify the query uses trigram operators
        call_args = mock_pool.fetch.call_args
        query = call_args[0][0]
        assert "%" in query or "similarity" in query.lower()


class TestGetRelease:
    """Tests for get_release method."""

    @pytest.mark.asyncio
    async def test_returns_full_metadata(self):
        """Test returns full release metadata by ID."""
        mock_pool = MagicMock()
        # Main release query
        mock_pool.fetchrow = AsyncMock(
            return_value={
                "id": 28138,
                "title": "Confield",
                "release_year": 2001,
                "artwork_url": "https://img.discogs.com/abc/cover.jpg",
            }
        )
        # Artists query
        mock_pool.fetch = AsyncMock(
            side_effect=[
                [{"artist_name": "Autechre", "extra": 0}],  # release_artist
                [  # release_track
                    {"position": "1", "title": "VI Scose Poise", "duration": "5:25", "sequence": 1},
                    {"position": "2", "title": "Cfern", "duration": "6:02", "sequence": 2},
                ],
                [],  # release_track_artist (no per-track artists)
            ]
        )

        from discogs.cache_service import DiscogsCacheService

        cache = DiscogsCacheService(pool=mock_pool)
        result = await cache.get_release(28138)

        assert result is not None
        assert isinstance(result, ReleaseMetadataResponse)
        assert result.release_id == 28138
        assert result.title == "Confield"
        assert result.artist == "Autechre"
        assert result.year == 2001
        assert result.artwork_url == "https://img.discogs.com/abc/cover.jpg"
        assert len(result.tracklist) == 2
        assert result.tracklist[0].title == "VI Scose Poise"

    @pytest.mark.asyncio
    async def test_not_found_returns_none(self):
        """Test returns None when release not found."""
        mock_pool = MagicMock()
        mock_pool.fetchrow = AsyncMock(return_value=None)

        from discogs.cache_service import DiscogsCacheService

        cache = DiscogsCacheService(pool=mock_pool)
        result = await cache.get_release(99999999)

        assert result is None


class TestWriteRelease:
    """Tests for write_release method (cache write-back)."""

    @pytest.mark.asyncio
    async def test_inserts_all_related_rows(self):
        """Test writes release, artists, and tracks to cache."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.executemany = AsyncMock()

        # Create a proper async context manager for acquire()
        mock_pool = MagicMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=None)
        mock_pool.acquire.return_value = mock_cm

        from discogs.cache_service import DiscogsCacheService

        cache = DiscogsCacheService(pool=mock_pool)

        release_data = ReleaseMetadataResponse(
            release_id=28138,
            title="Confield",
            artist="Autechre",
            year=2001,
            label="Warp Records",
            genres=["Electronic"],
            styles=["IDM"],
            tracklist=[
                TrackItem(position="1", title="VI Scose Poise", duration="5:25"),
                TrackItem(position="2", title="Cfern", duration="6:02"),
            ],
            release_url="https://www.discogs.com/release/28138",
        )

        await cache.write_release(release_data)

        # Verify execute was called (for release upsert)
        assert mock_conn.execute.called
        # Verify the release upsert uses the new column names
        first_call_query = mock_conn.execute.call_args_list[0][0][0]
        assert "release_year" in first_call_query
        assert "artwork_url" in first_call_query
        # Verify executemany was called (for tracks)
        assert mock_conn.executemany.called

    @pytest.mark.asyncio
    async def test_updates_existing_release(self):
        """Test upserts when release already exists (ON CONFLICT UPDATE)."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.executemany = AsyncMock()

        # Create a proper async context manager for acquire()
        mock_pool = MagicMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=None)
        mock_pool.acquire.return_value = mock_cm

        from discogs.cache_service import DiscogsCacheService

        cache = DiscogsCacheService(pool=mock_pool)

        release_data = ReleaseMetadataResponse(
            release_id=28138,
            title="Confield (Updated)",
            artist="Autechre",
            year=2001,
            release_url="https://www.discogs.com/release/28138",
        )

        await cache.write_release(release_data)

        # Verify upsert query contains ON CONFLICT
        call_args = mock_conn.execute.call_args
        query = call_args[0][0]
        assert "ON CONFLICT" in query


class TestCacheAvailability:
    """Tests for cache availability and health checks."""

    @pytest.mark.asyncio
    async def test_is_available_returns_true_when_healthy(self):
        """Test is_available returns True when DB responds."""
        mock_pool = MagicMock()
        mock_pool.fetchval = AsyncMock(return_value=1)

        from discogs.cache_service import DiscogsCacheService

        cache = DiscogsCacheService(pool=mock_pool)
        result = await cache.is_available()

        assert result is True
        mock_pool.fetchval.assert_called_once()

    @pytest.mark.asyncio
    async def test_is_available_returns_false_on_error(self):
        """Test is_available returns False when DB is unreachable."""
        mock_pool = MagicMock()
        mock_pool.fetchval = AsyncMock(side_effect=Exception("Connection failed"))

        from discogs.cache_service import DiscogsCacheService

        cache = DiscogsCacheService(pool=mock_pool)
        result = await cache.is_available()

        assert result is False

    @pytest.mark.asyncio
    async def test_connection_error_raises_cache_unavailable(self):
        """Test that connection errors raise CacheUnavailableError."""
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(side_effect=Exception("Connection refused"))

        from discogs.cache_service import CacheUnavailableError, DiscogsCacheService

        cache = DiscogsCacheService(pool=mock_pool)

        with pytest.raises(CacheUnavailableError):
            await cache.search_releases_by_track(track="Test", artist="Test")


class TestValidateTrackOnRelease:
    """Tests for validate_track_on_release method."""

    @pytest.mark.asyncio
    async def test_returns_true_when_track_found(self):
        """Test returns True when track by artist exists on release."""
        mock_pool = MagicMock()
        # Release exists
        mock_pool.fetchrow = AsyncMock(
            return_value={
                "id": 28138,
                "title": "Confield",
                "release_year": 2001,
                "artwork_url": None,
            }
        )
        # Track query returns match
        mock_pool.fetch = AsyncMock(
            side_effect=[
                [{"artist_name": "Autechre", "extra": 0}],
                [{"position": "1", "title": "VI Scose Poise", "duration": "5:25", "sequence": 1}],
                [],
            ]
        )

        from discogs.cache_service import DiscogsCacheService

        cache = DiscogsCacheService(pool=mock_pool)
        result = await cache.validate_track_on_release(28138, "VI Scose Poise", "Autechre")

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_track_not_found(self):
        """Test returns False when track not on release."""
        mock_pool = MagicMock()
        mock_pool.fetchrow = AsyncMock(
            return_value={
                "id": 28138,
                "title": "Confield",
                "release_year": 2001,
                "artwork_url": None,
            }
        )
        mock_pool.fetch = AsyncMock(
            side_effect=[
                [{"artist_name": "Autechre", "extra": 0}],
                [{"position": "1", "title": "Cfern", "duration": "6:02", "sequence": 1}],
                [],
            ]
        )

        from discogs.cache_service import DiscogsCacheService

        cache = DiscogsCacheService(pool=mock_pool)
        result = await cache.validate_track_on_release(28138, "VI Scose Poise", "Autechre")

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_none_when_release_not_cached(self):
        """Test returns None when release is not in cache (cache miss)."""
        mock_pool = MagicMock()
        mock_pool.fetchrow = AsyncMock(return_value=None)

        from discogs.cache_service import DiscogsCacheService

        cache = DiscogsCacheService(pool=mock_pool)
        result = await cache.validate_track_on_release(99999999, "Some Track", "Some Artist")

        # Returns None to indicate cache miss (caller should try API)
        assert result is None


class TestSearchReleases:
    """Tests for search_releases method (general release search by artist/album)."""

    @pytest.mark.asyncio
    async def test_search_by_artist_and_album(self):
        """Test search with both artist and album returns ranked results."""
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 28138,
                    "title": "Confield",
                    "artist_name": "Autechre",
                    "artwork_url": "https://img.discogs.com/abc/cover.jpg",
                    "score": 0.85,
                },
                {
                    "release_id": 55555,
                    "title": "Confield Remixes",
                    "artist_name": "Autechre",
                    "artwork_url": None,
                    "score": 0.6,
                },
            ]
        )

        from discogs.cache_service import DiscogsCacheService

        cache = DiscogsCacheService(pool=mock_pool)
        results = await cache.search_releases(artist="Autechre", album="Confield")

        assert len(results) == 2
        assert results[0]["release_id"] == 28138
        assert results[0]["title"] == "Confield"
        assert results[0]["artist_name"] == "Autechre"
        assert results[0]["artwork_url"] == "https://img.discogs.com/abc/cover.jpg"

    @pytest.mark.asyncio
    async def test_search_by_artist_only(self):
        """Test search with artist only (no album) returns results."""
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 28138,
                    "title": "Confield",
                    "artist_name": "Autechre",
                    "artwork_url": None,
                    "score": 0.9,
                },
            ]
        )

        from discogs.cache_service import DiscogsCacheService

        cache = DiscogsCacheService(pool=mock_pool)
        results = await cache.search_releases(artist="Autechre")

        assert len(results) == 1
        assert results[0]["artist_name"] == "Autechre"

    @pytest.mark.asyncio
    async def test_search_by_album_only(self):
        """Test search with album only (no artist) returns results."""
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 28138,
                    "title": "Confield",
                    "artist_name": "Autechre",
                    "artwork_url": None,
                    "score": 0.8,
                },
            ]
        )

        from discogs.cache_service import DiscogsCacheService

        cache = DiscogsCacheService(pool=mock_pool)
        results = await cache.search_releases(album="Confield")

        assert len(results) == 1
        assert results[0]["title"] == "Confield"

    @pytest.mark.asyncio
    async def test_search_no_params_returns_empty(self):
        """Test search with no artist or album returns empty list."""
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(return_value=[])

        from discogs.cache_service import DiscogsCacheService

        cache = DiscogsCacheService(pool=mock_pool)
        results = await cache.search_releases()

        assert results == []
        # Should not even query the database
        mock_pool.fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_search_deduplicates_results(self):
        """Test that duplicate albums are deduplicated."""
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 28138,
                    "title": "Confield",
                    "artist_name": "Autechre",
                    "artwork_url": None,
                    "score": 0.9,
                },
                {
                    "release_id": 99999,
                    "title": "Confield",
                    "artist_name": "Autechre",
                    "artwork_url": None,
                    "score": 0.85,
                },
            ]
        )

        from discogs.cache_service import DiscogsCacheService

        cache = DiscogsCacheService(pool=mock_pool)
        results = await cache.search_releases(artist="Autechre", album="Confield")

        assert len(results) == 1
        assert results[0]["release_id"] == 28138

    @pytest.mark.asyncio
    async def test_search_cache_unavailable_raises(self):
        """Test that connection errors raise CacheUnavailableError."""
        mock_pool = MagicMock()
        mock_pool.fetch = AsyncMock(side_effect=Exception("Connection refused"))

        from discogs.cache_service import CacheUnavailableError, DiscogsCacheService

        cache = DiscogsCacheService(pool=mock_pool)

        with pytest.raises(CacheUnavailableError):
            await cache.search_releases(artist="Autechre", album="Confield")


class TestBuildSearchQuery:
    """Tests for _build_search_query helper that generates parameterized SQL."""

    def _build(self, artist=None, album=None, limit=5):
        from discogs.cache_service import DiscogsCacheService

        cache = DiscogsCacheService(pool=MagicMock())
        return cache._build_search_query(artist=artist, album=album, limit=limit)

    def test_artist_and_album_includes_both_conditions(self):
        """Test both artist and album generate WHERE with both conditions."""
        query, params = self._build(artist="Autechre", album="Confield")
        assert "lower(r.title)" in query
        assert "lower(ra.artist_name)" in query
        assert "GREATEST" in query
        assert params == ["Confield", "Autechre", 10]  # limit * 2

    def test_artist_only_excludes_album(self):
        """Test artist-only search does not reference album similarity."""
        query, params = self._build(artist="Autechre")
        assert "lower(ra.artist_name)" in query
        assert "GREATEST" not in query
        assert params == ["Autechre", 10]

    def test_album_only_excludes_artist(self):
        """Test album-only search does not reference artist similarity."""
        query, params = self._build(album="Confield")
        assert "lower(r.title)" in query
        assert "GREATEST" not in query
        assert params == ["Confield", 10]

    def test_limit_is_doubled(self):
        """Test that limit parameter is doubled for the SQL LIMIT clause."""
        _, params = self._build(artist="Autechre", album="Confield", limit=3)
        assert params[-1] == 6  # 3 * 2

    def test_returns_sql_with_limit_placeholder(self):
        """Test returned SQL uses correct positional placeholders."""
        query, params = self._build(artist="Autechre", album="Confield")
        # Should have $3 for LIMIT (2 search params + 1 limit)
        assert f"${len(params)}" in query

    def test_artist_only_returns_correct_placeholder_count(self):
        """Test artist-only uses $1 for artist and $2 for limit."""
        query, params = self._build(artist="Autechre")
        assert "$1" in query
        assert "$2" in query
        assert "$3" not in query
