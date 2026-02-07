"""Telemetry module for tracking request performance with PostHog."""

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from posthog import Posthog

logger = logging.getLogger(__name__)

DISTINCT_ID = "request-o-matic-service"


@dataclass
class StepResult:
    """Result of a tracked step."""

    duration_ms: float
    success: bool = True
    error_type: str | None = None


@dataclass
class CacheStats:
    """Statistics for cache operations."""

    hits: int = 0
    misses: int = 0
    writes: int = 0
    errors: int = 0

    @property
    def total_queries(self) -> int:
        """Total number of cache queries (hits + misses)."""
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        """Cache hit rate as a percentage (0.0 to 1.0)."""
        if self.total_queries == 0:
            return 0.0
        return self.hits / self.total_queries


@dataclass
class RequestTelemetry:
    """Tracks performance metrics for a single request."""

    steps: dict[str, StepResult] = field(default_factory=dict)
    api_calls: dict[str, int] = field(default_factory=lambda: {"groq": 0, "discogs": 0, "slack": 0})
    cache_stats: CacheStats = field(default_factory=CacheStats)
    start_time: float = field(default_factory=time.perf_counter)
    _current_step: str | None = field(default=None, repr=False)
    _step_start: float = field(default=0.0, repr=False)

    @contextmanager
    def track_step(self, step_name: str):
        """Context manager to time a step.

        Args:
            step_name: Name of the step being tracked

        Yields:
            None

        Example:
            with telemetry.track_step("parse"):
                result = parse_request(message, client)
        """
        self._current_step = step_name
        self._step_start = time.perf_counter()
        error_type = None

        try:
            yield
        except Exception as e:
            error_type = type(e).__name__
            raise
        finally:
            duration_ms = (time.perf_counter() - self._step_start) * 1000
            self.steps[step_name] = StepResult(
                duration_ms=duration_ms,
                success=error_type is None,
                error_type=error_type,
            )
            self._current_step = None

    def record_api_call(self, service: str) -> None:
        """Increment API call counter for a service.

        Args:
            service: Name of the service ("groq", "discogs", or "slack")
        """
        if service in self.api_calls:
            self.api_calls[service] += 1
        else:
            logger.warning(f"Unknown service for API call tracking: {service}")

    def record_cache_hit(self) -> None:
        """Record a cache hit."""
        self.cache_stats.hits += 1

    def record_cache_miss(self) -> None:
        """Record a cache miss."""
        self.cache_stats.misses += 1

    def record_cache_write(self) -> None:
        """Record a cache write operation."""
        self.cache_stats.writes += 1

    def record_cache_error(self) -> None:
        """Record a cache error (connection failure, etc.)."""
        self.cache_stats.errors += 1

    def get_total_duration_ms(self) -> float:
        """Get total elapsed time since telemetry was created."""
        return (time.perf_counter() - self.start_time) * 1000

    def get_step_timings(self) -> dict[str, float]:
        """Get timing for each step in milliseconds."""
        return {f"{name}_ms": step.duration_ms for name, step in self.steps.items()}

    def send_to_posthog(
        self,
        posthog_client: Posthog,
        extra_properties: dict[str, Any] | None = None,
    ) -> None:
        """Send all telemetry events to PostHog.

        Sends individual step events and a final summary event.

        Args:
            posthog_client: PostHog client instance
            extra_properties: Additional properties to include in the completed event
        """
        extra_properties = extra_properties or {}

        # Send individual step events
        for step_name, step_result in self.steps.items():
            posthog_client.capture(
                distinct_id=DISTINCT_ID,
                event=f"request_{step_name}",
                properties={
                    "step": step_name,
                    "duration_ms": round(step_result.duration_ms, 2),
                    "success": step_result.success,
                    "error_type": step_result.error_type,
                },
            )

        # Send summary event
        posthog_client.capture(
            distinct_id=DISTINCT_ID,
            event="request_completed",
            properties={
                "total_duration_ms": round(self.get_total_duration_ms(), 2),
                "steps": self.get_step_timings(),
                "api_calls": self.api_calls.copy(),
                "cache": {
                    "hits": self.cache_stats.hits,
                    "misses": self.cache_stats.misses,
                    "writes": self.cache_stats.writes,
                    "errors": self.cache_stats.errors,
                    "hit_rate": round(self.cache_stats.hit_rate, 2),
                },
                **extra_properties,
            },
        )

        logger.debug(
            f"Sent telemetry: {len(self.steps)} steps, total {self.get_total_duration_ms():.1f}ms"
        )


# ---------------------------------------------------------------------------
# Per-request cache stats via ContextVar
# ---------------------------------------------------------------------------

_cache_stats_var: ContextVar[dict] = ContextVar("cache_stats")


def init_cache_stats() -> None:
    """Initialize cache stats for the current request context."""
    _cache_stats_var.set({"memory_hits": 0, "pg_hits": 0, "pg_misses": 0, "api_calls": 0})


def record_memory_cache_hit() -> None:
    """Record an in-memory TTL cache hit in the current request context."""
    stats = _cache_stats_var.get(None)
    if stats is not None:
        stats["memory_hits"] += 1


def record_pg_cache_hit() -> None:
    """Record a PostgreSQL cache hit in the current request context."""
    stats = _cache_stats_var.get(None)
    if stats is not None:
        stats["pg_hits"] += 1


def record_pg_cache_miss() -> None:
    """Record a PostgreSQL cache miss in the current request context."""
    stats = _cache_stats_var.get(None)
    if stats is not None:
        stats["pg_misses"] += 1


def record_discogs_api_call() -> None:
    """Record a Discogs API call in the current request context."""
    stats = _cache_stats_var.get(None)
    if stats is not None:
        stats["api_calls"] += 1


def get_cache_stats() -> dict | None:
    """Get cache stats for the current request context, or None if not initialized."""
    return _cache_stats_var.get(None)
