"""Caching utilities for Discogs API responses using TTL-based LRU cache."""

import hashlib
import json
import logging
from collections.abc import Callable
from contextvars import ContextVar
from functools import wraps
from typing import Any, TypeVar

from cachetools import TTLCache  # type: ignore[import-untyped]
from pydantic import BaseModel

from core.telemetry import record_memory_cache_hit, record_memory_cache_miss

logger = logging.getLogger(__name__)

# Registry of all caches for bulk operations
_cache_registry: list[TTLCache] = []

# Table-driven cache configuration: name -> (maxsize_divisor, ttl_setting_attr)
_CACHE_CONFIGS: dict[str, tuple[int, str]] = {
    "track": (1, "discogs_track_cache_ttl"),
    "release": (2, "discogs_release_cache_ttl"),
    "search": (1, "discogs_search_cache_ttl"),
    "artist": (2, "discogs_artist_cache_ttl"),
    "label": (2, "discogs_label_cache_ttl"),
}

# Lazily-initialized cache instances (keyed by name from _CACHE_CONFIGS)
_caches: dict[str, TTLCache] = {}

T = TypeVar("T")

# Per-request flag to bypass all caches (in-memory and PG).
# Used for benchmarking and A/B cache comparisons.
_skip_cache_var: ContextVar[bool] = ContextVar("skip_cache", default=False)


def set_skip_cache(skip: bool) -> None:
    """Set the per-request skip_cache flag."""
    _skip_cache_var.set(skip)


def should_skip_cache() -> bool:
    """Check whether caches should be bypassed for the current request."""
    return _skip_cache_var.get(False)


def make_cache_key(func_name: str, *args, **kwargs) -> str:
    """Generate a deterministic cache key from function name and arguments.

    Uses JSON serialization for consistent, deterministic key generation.
    Args and kwargs are normalized to ensure same inputs produce same keys.

    Args:
        func_name: Name of the function being cached
        *args: Positional arguments to the function
        **kwargs: Keyword arguments to the function

    Returns:
        MD5 hash of the serialized arguments
    """
    key_data = {
        "fn": func_name,
        "args": list(args),
        "kwargs": dict(sorted(kwargs.items())),
    }
    key_string = json.dumps(key_data, sort_keys=True, default=str)
    return hashlib.md5(key_string.encode()).hexdigest()


def create_ttl_cache(maxsize: int, ttl: int) -> TTLCache:
    """Create a TTL cache and register it for bulk operations.

    Args:
        maxsize: Maximum number of entries in the cache
        ttl: Time-to-live in seconds for cache entries

    Returns:
        TTLCache instance
    """
    cache = TTLCache(maxsize=maxsize, ttl=ttl)
    _cache_registry.append(cache)
    return cache


def clear_all_caches() -> None:
    """Clear all registered caches and reset lazy caches."""
    for cache in _cache_registry:
        cache.clear()
    # Reset lazy caches so they get recreated with fresh settings
    _caches.clear()


def _set_cached_flag(result: Any, cached: bool) -> Any:
    """Set the cached flag on a result if it has one.

    Works with both dicts and Pydantic models.
    """
    if result is None:
        return result

    if isinstance(result, dict) and "cached" in result:
        result = result.copy()
        result["cached"] = cached
        return result

    if isinstance(result, BaseModel) and hasattr(result, "cached"):
        return result.model_copy(update={"cached": cached})

    return result


def async_cached(cache: TTLCache) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator for caching async function results.

    The decorated function's results are cached based on its arguments.
    If the result has a 'cached' field, it will be set to True on cache hits.
    None results are not cached.

    Args:
        cache: TTLCache instance to use for caching

    Returns:
        Decorator function
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            # Bypass cache entirely when skip_cache flag is set
            if should_skip_cache():
                return await func(*args, **kwargs)  # type: ignore[misc, no-any-return]

            # Generate cache key from function name and arguments
            # Skip 'self' if present (first arg of instance methods)
            # Check if first arg has this method, indicating it's 'self'
            cache_args = args
            if args and hasattr(args[0], func.__name__):
                cache_args = args[1:]

            key = make_cache_key(func.__name__, *cache_args, **kwargs)

            # Check cache
            if key in cache:
                logger.debug(f"Cache hit for {func.__name__}")
                record_memory_cache_hit()
                result = cache[key]
                return _set_cached_flag(result, cached=True)  # type: ignore[no-any-return]

            # Cache miss - call function
            logger.debug(f"Cache miss for {func.__name__}")
            record_memory_cache_miss()
            result = await func(*args, **kwargs)  # type: ignore[misc]

            # Don't cache None results
            if result is not None:
                cache[key] = result

            return result  # type: ignore[no-any-return]

        return wrapper  # type: ignore[return-value]

    return decorator


def _get_cache(name: str) -> TTLCache:
    """Get or create a named cache using settings from _CACHE_CONFIGS.

    Imports settings lazily to avoid circular imports at module load time.

    Args:
        name: Cache name (must be a key in _CACHE_CONFIGS)

    Returns:
        TTLCache instance (created on first access, reused thereafter)
    """
    if name not in _caches:
        from config.settings import get_settings

        maxsize_divisor, ttl_attr = _CACHE_CONFIGS[name]
        settings = get_settings()
        _caches[name] = create_ttl_cache(
            maxsize=settings.discogs_cache_maxsize // maxsize_divisor,
            ttl=getattr(settings, ttl_attr),
        )
    return _caches[name]


class CacheRegistry:
    """Provides lazy, IDE-friendly access to named TTL caches.

    Each property returns the corresponding cache, creating it on first access.
    Replaces the previous module-level ``__getattr__`` approach which lacked
    IDE auto-completion and was hard to discover.
    """

    @property
    def track(self) -> TTLCache:
        """Get or create the track search cache."""
        return _get_cache("track")

    @property
    def release(self) -> TTLCache:
        """Get or create the release metadata cache."""
        return _get_cache("release")

    @property
    def search(self) -> TTLCache:
        """Get or create the general search cache."""
        return _get_cache("search")

    @property
    def artist(self) -> TTLCache:
        """Get or create the artist image cache."""
        return _get_cache("artist")

    @property
    def label(self) -> TTLCache:
        """Get or create the label image cache."""
        return _get_cache("label")

    def clear_all(self) -> None:
        """Clear all registered caches and reset lazy instances."""
        clear_all_caches()


caches = CacheRegistry()
