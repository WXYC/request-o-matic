"""Tests for Discogs rate limiting utilities."""

import asyncio

import pytest

from discogs.ratelimit import (
    get_rate_limiter,
    get_semaphore,
    reset_rate_limiting,
)


@pytest.fixture(autouse=True)
def reset_state():
    """Reset rate limiting state before and after each test."""
    reset_rate_limiting()
    yield
    reset_rate_limiting()


class TestGetSemaphore:
    """Tests for get_semaphore function."""

    def test_returns_semaphore(self, monkeypatch):
        """Test returns an asyncio.Semaphore."""
        monkeypatch.setenv("GROQ_API_KEY", "test-key")
        semaphore = get_semaphore()
        assert isinstance(semaphore, asyncio.Semaphore)

    @pytest.mark.asyncio
    async def test_returns_same_instance(self, monkeypatch):
        """Test returns the same semaphore instance on subsequent calls within same event loop."""
        monkeypatch.setenv("GROQ_API_KEY", "test-key")
        sem1 = get_semaphore()
        sem2 = get_semaphore()
        assert sem1 is sem2

    @pytest.mark.asyncio
    async def test_reset_creates_new_instance(self, monkeypatch):
        """Test reset creates a new semaphore on next access."""
        monkeypatch.setenv("GROQ_API_KEY", "test-key")
        sem1 = get_semaphore()
        reset_rate_limiting()
        sem2 = get_semaphore()
        assert sem1 is not sem2


class TestGetRateLimiter:
    """Tests for get_rate_limiter function."""

    def test_returns_rate_limiter(self, monkeypatch):
        """Test returns an AsyncLimiter."""
        monkeypatch.setenv("GROQ_API_KEY", "test-key")
        from aiolimiter import AsyncLimiter

        limiter = get_rate_limiter()
        assert isinstance(limiter, AsyncLimiter)

    @pytest.mark.asyncio
    async def test_returns_same_instance(self, monkeypatch):
        """Test returns the same limiter instance on subsequent calls within same event loop."""
        monkeypatch.setenv("GROQ_API_KEY", "test-key")
        lim1 = get_rate_limiter()
        lim2 = get_rate_limiter()
        assert lim1 is lim2

    @pytest.mark.asyncio
    async def test_reset_creates_new_instance(self, monkeypatch):
        """Test reset creates a new limiter on next access."""
        monkeypatch.setenv("GROQ_API_KEY", "test-key")
        lim1 = get_rate_limiter()
        reset_rate_limiting()
        lim2 = get_rate_limiter()
        assert lim1 is not lim2


class TestSemaphoreConcurrency:
    """Tests for semaphore concurrency limiting."""

    @pytest.mark.asyncio
    async def test_limits_concurrent_access(self, monkeypatch):
        """Test semaphore limits concurrent access."""
        monkeypatch.setenv("GROQ_API_KEY", "test-key")
        monkeypatch.setenv("DISCOGS_MAX_CONCURRENT", "2")

        # Clear settings cache so new env vars are picked up
        from config.settings import get_settings

        get_settings.cache_clear()
        reset_rate_limiting()  # Recreate with new settings

        semaphore = get_semaphore()
        active_count = 0
        max_active = 0

        async def task():
            nonlocal active_count, max_active
            async with semaphore:
                active_count += 1
                max_active = max(max_active, active_count)
                await asyncio.sleep(0.01)
                active_count -= 1

        # Run 5 concurrent tasks with semaphore limit of 2
        await asyncio.gather(*[task() for _ in range(5)])

        # Clean up settings cache
        get_settings.cache_clear()

        assert max_active <= 2


class TestRateLimiterAcquisition:
    """Tests for rate limiter token acquisition."""

    @pytest.mark.asyncio
    async def test_acquires_token(self, monkeypatch):
        """Test rate limiter allows token acquisition."""
        monkeypatch.setenv("GROQ_API_KEY", "test-key")
        limiter = get_rate_limiter()

        # Should complete without blocking
        await asyncio.wait_for(limiter.acquire(), timeout=1.0)
