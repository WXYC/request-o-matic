"""Telemetry module for tracking request performance with PostHog."""

import logging
import time
from contextlib import contextmanager
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
class RequestTelemetry:
    """Tracks performance metrics for a single request."""

    steps: dict[str, StepResult] = field(default_factory=dict)
    api_calls: dict[str, int] = field(default_factory=lambda: {"groq": 0, "discogs": 0, "slack": 0})
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
                **extra_properties,
            },
        )

        logger.debug(
            f"Sent telemetry: {len(self.steps)} steps, "
            f"total {self.get_total_duration_ms():.1f}ms"
        )
