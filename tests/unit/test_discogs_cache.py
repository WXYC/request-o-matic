"""Tests for Discogs caching layer."""

from typing import Optional

import pytest

from discogs.cache import async_cached, make_cache_key, create_ttl_cache, clear_all_caches

# Default test data
TEST_TRACK = "VI Scose Poise"
TEST_ALBUM = "Confield"
TEST_ARTIST = "Autechre"


class TestMakeCacheKey:
    """Tests for cache key generation."""

    def test_simple_args(self):
        """Test cache key with simple string arguments."""
        key1 = make_cache_key("search_releases", TEST_TRACK, TEST_ARTIST)
        key2 = make_cache_key("search_releases", TEST_TRACK, TEST_ARTIST)
        assert key1 == key2

    def test_different_args_different_keys(self):
        """Test different arguments produce different keys."""
        key1 = make_cache_key("search_releases", TEST_TRACK)
        key2 = make_cache_key("search_releases", "Different Track")
        assert key1 != key2

    def test_kwargs_order_independent(self):
        """Test kwargs produce same key regardless of order."""
        key1 = make_cache_key("search", artist=TEST_ARTIST, album=TEST_ALBUM)
        key2 = make_cache_key("search", album=TEST_ALBUM, artist=TEST_ARTIST)
        assert key1 == key2

    def test_none_values_handled(self):
        """Test None values are handled correctly."""
        key1 = make_cache_key("search_releases", TEST_TRACK, None)
        key2 = make_cache_key("search_releases", TEST_TRACK, None)
        assert key1 == key2

    def test_different_function_names_different_keys(self):
        """Test same args but different function names produce different keys."""
        key1 = make_cache_key("search_releases", TEST_TRACK)
        key2 = make_cache_key("get_release", TEST_TRACK)
        assert key1 != key2

    def test_deterministic_across_calls(self):
        """Test cache key is deterministic (same input always produces same key)."""
        # Call multiple times to ensure consistency
        keys = [make_cache_key("search", TEST_TRACK, artist=TEST_ARTIST) for _ in range(10)]
        assert len(set(keys)) == 1  # All keys should be identical

    def test_integer_args(self):
        """Test cache key works with integer arguments."""
        key1 = make_cache_key("get_release", 12345)
        key2 = make_cache_key("get_release", 12345)
        assert key1 == key2

        key3 = make_cache_key("get_release", 99999)
        assert key1 != key3


class TestCreateTtlCache:
    """Tests for TTL cache creation."""

    def test_creates_cache_with_maxsize(self):
        """Test cache is created with specified maxsize."""
        cache = create_ttl_cache(maxsize=100, ttl=3600)
        assert cache.maxsize == 100

    def test_creates_cache_with_ttl(self):
        """Test cache is created with specified TTL."""
        cache = create_ttl_cache(maxsize=100, ttl=3600)
        assert cache.ttl == 3600


class TestAsyncCached:
    """Tests for async_cached decorator."""

    @pytest.fixture(autouse=True)
    def clear_caches(self):
        """Clear all caches before each test."""
        clear_all_caches()
        yield
        clear_all_caches()

    @pytest.mark.asyncio
    async def test_cache_miss_calls_function(self):
        """Test function is called on cache miss."""
        cache = create_ttl_cache(maxsize=100, ttl=3600)
        call_count = 0

        @async_cached(cache)
        async def my_func(track: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"track": track, "cached": False}

        result = await my_func(TEST_TRACK)
        assert result["track"] == TEST_TRACK
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_value(self):
        """Test cached value is returned on cache hit."""
        cache = create_ttl_cache(maxsize=100, ttl=3600)
        call_count = 0

        @async_cached(cache)
        async def my_func(track: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"track": track, "cached": False}

        # First call - cache miss
        result1 = await my_func(TEST_TRACK)
        # Second call - cache hit
        result2 = await my_func(TEST_TRACK)

        assert result1["track"] == TEST_TRACK
        assert result2["track"] == TEST_TRACK
        assert call_count == 1  # Function only called once

    @pytest.mark.asyncio
    async def test_cache_hit_sets_cached_flag(self):
        """Test cached flag is set to True on cache hit."""
        cache = create_ttl_cache(maxsize=100, ttl=3600)

        @async_cached(cache)
        async def my_func(track: str) -> dict:
            return {"track": track, "cached": False}

        # First call - cache miss
        result1 = await my_func(TEST_TRACK)
        assert result1["cached"] is False

        # Second call - cache hit, should have cached=True
        result2 = await my_func(TEST_TRACK)
        assert result2["cached"] is True

    @pytest.mark.asyncio
    async def test_different_args_separate_cache_entries(self):
        """Test different arguments create separate cache entries."""
        cache = create_ttl_cache(maxsize=100, ttl=3600)
        call_count = 0

        @async_cached(cache)
        async def my_func(track: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"track": track, "cached": False}

        await my_func(TEST_TRACK)
        await my_func("Cfern")  # Another Autechre track
        await my_func(TEST_TRACK)  # Should be cached
        await my_func("Cfern")  # Should be cached

        assert call_count == 2  # Only called twice (once per unique arg)

    @pytest.mark.asyncio
    async def test_lru_eviction(self):
        """Test LRU eviction when cache is full."""
        cache = create_ttl_cache(maxsize=2, ttl=3600)
        call_count = 0

        @async_cached(cache)
        async def my_func(track: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"track": track, "cached": False}

        await my_func("Pen Expers")  # Cache: [Pen Expers]
        await my_func("Cfern")  # Cache: [Pen Expers, Cfern]
        await my_func(TEST_TRACK)  # Cache: [Cfern, VI Scose Poise] - 'Pen Expers' evicted
        await my_func("Pen Expers")  # Should be called again since it was evicted

        assert call_count == 4

    @pytest.mark.asyncio
    async def test_kwargs_caching(self):
        """Test caching works with keyword arguments."""
        cache = create_ttl_cache(maxsize=100, ttl=3600)
        call_count = 0

        @async_cached(cache)
        async def my_func(track: str, artist: Optional[str] = None) -> dict:
            nonlocal call_count
            call_count += 1
            return {"track": track, "artist": artist, "cached": False}

        await my_func(TEST_TRACK, artist=TEST_ARTIST)
        await my_func(TEST_TRACK, artist=TEST_ARTIST)  # Cache hit
        await my_func(TEST_TRACK, artist="Boards of Canada")  # Different artist, cache miss

        assert call_count == 2

    @pytest.mark.asyncio
    async def test_pydantic_model_cached_field(self):
        """Test caching works with Pydantic models that have a cached field."""
        from pydantic import BaseModel

        class MyResponse(BaseModel):
            track: str
            cached: bool = False

        cache = create_ttl_cache(maxsize=100, ttl=3600)

        @async_cached(cache)
        async def my_func(track: str) -> MyResponse:
            return MyResponse(track=track, cached=False)

        result1 = await my_func(TEST_TRACK)
        assert result1.cached is False

        result2 = await my_func(TEST_TRACK)
        assert result2.cached is True

    @pytest.mark.asyncio
    async def test_none_result_not_cached(self):
        """Test None results are not cached."""
        cache = create_ttl_cache(maxsize=100, ttl=3600)
        call_count = 0

        @async_cached(cache)
        async def my_func(track: str) -> Optional[dict]:
            nonlocal call_count
            call_count += 1
            if track == "nonexistent":
                return None
            return {"track": track, "cached": False}

        await my_func("nonexistent")
        await my_func("nonexistent")  # Should call again since None wasn't cached

        assert call_count == 2


class TestClearAllCaches:
    """Tests for clearing all registered caches."""

    @pytest.mark.asyncio
    async def test_clear_all_caches(self):
        """Test clearing all registered caches."""
        clear_all_caches()  # Start fresh

        cache1 = create_ttl_cache(maxsize=100, ttl=3600)
        cache2 = create_ttl_cache(maxsize=100, ttl=3600)
        call_count = 0

        @async_cached(cache1)
        async def func1(track: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"track": track, "cached": False}

        @async_cached(cache2)
        async def func2(track: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"track": track, "cached": False}

        await func1(TEST_TRACK)
        await func2(TEST_TRACK)
        assert call_count == 2

        # Clear all caches
        clear_all_caches()

        # Should be cache misses again
        await func1(TEST_TRACK)
        await func2(TEST_TRACK)
        assert call_count == 4
