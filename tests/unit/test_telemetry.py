"""Unit tests for the telemetry module."""

import time
from unittest.mock import MagicMock

import pytest

from core.telemetry import RequestTelemetry, StepResult


class TestRequestTelemetry:
    """Tests for RequestTelemetry class."""

    def test_track_step_records_timing(self):
        """Verify step timing is captured correctly."""
        telemetry = RequestTelemetry()

        with telemetry.track_step("parse"):
            time.sleep(0.05)  # 50ms

        assert "parse" in telemetry.steps
        assert telemetry.steps["parse"].duration_ms >= 50
        assert telemetry.steps["parse"].success is True
        assert telemetry.steps["parse"].error_type is None

    def test_track_step_records_error(self):
        """Verify errors are captured in step tracking."""
        telemetry = RequestTelemetry()

        with pytest.raises(ValueError):
            with telemetry.track_step("parse"):
                raise ValueError("test error")

        assert "parse" in telemetry.steps
        assert telemetry.steps["parse"].success is False
        assert telemetry.steps["parse"].error_type == "ValueError"

    def test_track_multiple_steps(self):
        """Verify multiple steps can be tracked."""
        telemetry = RequestTelemetry()

        with telemetry.track_step("parse"):
            time.sleep(0.01)

        with telemetry.track_step("search"):
            time.sleep(0.01)

        with telemetry.track_step("artwork"):
            time.sleep(0.01)

        assert len(telemetry.steps) == 3
        assert "parse" in telemetry.steps
        assert "search" in telemetry.steps
        assert "artwork" in telemetry.steps

    def test_api_call_counting(self):
        """Verify API calls are counted per service."""
        telemetry = RequestTelemetry()

        telemetry.record_api_call("discogs")
        telemetry.record_api_call("discogs")
        telemetry.record_api_call("groq")
        telemetry.record_api_call("slack")

        assert telemetry.api_calls["discogs"] == 2
        assert telemetry.api_calls["groq"] == 1
        assert telemetry.api_calls["slack"] == 1

    def test_api_call_unknown_service(self):
        """Verify unknown services are handled gracefully."""
        telemetry = RequestTelemetry()

        # Should not raise, just log a warning
        telemetry.record_api_call("unknown_service")

        # Original counters unchanged
        assert telemetry.api_calls["discogs"] == 0
        assert telemetry.api_calls["groq"] == 0
        assert telemetry.api_calls["slack"] == 0

    def test_get_total_duration(self):
        """Verify total duration calculation."""
        telemetry = RequestTelemetry()

        time.sleep(0.05)  # 50ms

        total = telemetry.get_total_duration_ms()
        assert total >= 50

    def test_get_step_timings(self):
        """Verify step timings are returned correctly."""
        telemetry = RequestTelemetry()

        with telemetry.track_step("parse"):
            time.sleep(0.01)

        with telemetry.track_step("search"):
            time.sleep(0.01)

        timings = telemetry.get_step_timings()

        assert "parse_ms" in timings
        assert "search_ms" in timings
        assert timings["parse_ms"] >= 10
        assert timings["search_ms"] >= 10


class TestSendToPosthog:
    """Tests for PostHog integration."""

    def test_send_to_posthog_captures_all_steps(self):
        """Verify all step events are sent to PostHog."""
        telemetry = RequestTelemetry()
        mock_posthog = MagicMock()

        with telemetry.track_step("parse"):
            pass

        with telemetry.track_step("search"):
            pass

        telemetry.send_to_posthog(mock_posthog, {})

        # Should send: request_parse, request_search, request_completed
        assert mock_posthog.capture.call_count == 3

        # Check event names
        event_names = [call.kwargs["event"] for call in mock_posthog.capture.call_args_list]
        assert "request_parse" in event_names
        assert "request_search" in event_names
        assert "request_completed" in event_names

    def test_send_to_posthog_includes_step_properties(self):
        """Verify step events include correct properties."""
        telemetry = RequestTelemetry()
        mock_posthog = MagicMock()

        with telemetry.track_step("parse"):
            time.sleep(0.01)

        telemetry.send_to_posthog(mock_posthog, {})

        # Find the parse event
        parse_call = next(
            call
            for call in mock_posthog.capture.call_args_list
            if call.kwargs["event"] == "request_parse"
        )

        props = parse_call.kwargs["properties"]
        assert props["step"] == "parse"
        assert props["duration_ms"] >= 10
        assert props["success"] is True
        assert props["error_type"] is None

    def test_send_to_posthog_includes_completed_summary(self):
        """Verify completed event includes summary data."""
        telemetry = RequestTelemetry()
        mock_posthog = MagicMock()

        telemetry.record_api_call("groq")
        telemetry.record_api_call("discogs")

        with telemetry.track_step("parse"):
            pass

        telemetry.send_to_posthog(mock_posthog, {"results_count": 5})

        # Find the completed event
        completed_call = next(
            call
            for call in mock_posthog.capture.call_args_list
            if call.kwargs["event"] == "request_completed"
        )

        props = completed_call.kwargs["properties"]
        assert "total_duration_ms" in props
        assert "steps" in props
        assert "api_calls" in props
        assert props["api_calls"]["groq"] == 1
        assert props["api_calls"]["discogs"] == 1
        assert props["results_count"] == 5

    def test_send_to_posthog_uses_correct_distinct_id(self):
        """Verify the correct distinct_id is used."""
        telemetry = RequestTelemetry()
        mock_posthog = MagicMock()

        with telemetry.track_step("parse"):
            pass

        telemetry.send_to_posthog(mock_posthog, {})

        # All calls should use the same distinct_id
        for call in mock_posthog.capture.call_args_list:
            assert call.kwargs["distinct_id"] == "request-o-matic-service"


class TestStepResult:
    """Tests for StepResult dataclass."""

    def test_default_values(self):
        """Verify default values are set correctly."""
        result = StepResult(duration_ms=100.0)

        assert result.duration_ms == 100.0
        assert result.success is True
        assert result.error_type is None

    def test_error_values(self):
        """Verify error state is captured correctly."""
        result = StepResult(duration_ms=50.0, success=False, error_type="ValueError")

        assert result.duration_ms == 50.0
        assert result.success is False
        assert result.error_type == "ValueError"
