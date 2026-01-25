"""
Baseline management for performance tests.

Tracks performance baselines per environment (local, staging, prod) and
compares current test runs against recorded baselines.
"""

import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional, cast


class Environment(Enum):
    """Test environment."""

    LOCAL = "local"
    STAGING = "staging"
    PROD = "prod"


def detect_environment(base_url: str) -> Environment:
    """Detect environment from the base URL."""
    if "localhost" in base_url or "127.0.0.1" in base_url:
        return Environment.LOCAL
    elif "staging" in base_url:
        return Environment.STAGING
    else:
        return Environment.PROD


class BaselineManager:
    """Manages performance baselines stored in a JSON file."""

    def __init__(self, baseline_path: Optional[Path] = None):
        if baseline_path is None:
            baseline_path = Path(__file__).parent / "baselines.json"
        self.baseline_path = baseline_path
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        """Load baselines from JSON file."""
        if not self.baseline_path.exists():
            return {}
        try:
            with open(self.baseline_path) as f:
                return cast(dict[str, Any], json.load(f))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self) -> None:
        """Save baselines to JSON file."""
        with open(self.baseline_path, "w") as f:
            json.dump(self.data, f, indent=2)

    def get_baseline(self, env: Environment, test_name: str) -> Optional[dict[str, Any]]:
        """Get baseline for a specific environment and test."""
        env_data = self.data.get(env.value, {})
        return cast(Optional[dict[str, Any]], env_data.get(test_name))

    def save_baseline(
        self,
        env: Environment,
        test_name: str,
        mean_ms: float,
        min_ms: float,
        max_ms: float,
        query_count: int,
    ) -> None:
        """Save a new baseline."""
        if env.value not in self.data:
            self.data[env.value] = {}

        self.data[env.value][test_name] = {
            "mean_ms": round(mean_ms, 1),
            "min_ms": round(min_ms, 1),
            "max_ms": round(max_ms, 1),
            "query_count": query_count,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()


class PerformanceRegression(Exception):
    """Raised when performance regresses beyond the threshold."""

    pass


def check_performance(
    metrics_summary: dict,
    test_name: str,
    environment: Environment,
    baseline_manager: BaselineManager,
    threshold_pct: float = 20.0,
) -> None:
    """
    Compare current metrics against baseline.

    - If no baseline exists, saves current metrics as baseline.
    - If baseline exists, compares and raises PerformanceRegression if
      the mean exceeds the baseline by more than threshold_pct.

    Args:
        metrics_summary: Dict with mean_ms, min_ms, max_ms, count keys
        test_name: Name of the test (used as baseline key)
        environment: Current test environment
        baseline_manager: Manager instance for loading/saving baselines
        threshold_pct: Maximum allowed regression percentage (default 20%)

    Raises:
        PerformanceRegression: If current mean exceeds baseline by threshold
    """
    current_mean = metrics_summary["mean_ms"]
    current_min = metrics_summary["min_ms"]
    current_max = metrics_summary["max_ms"]
    query_count = metrics_summary["count"]

    baseline = baseline_manager.get_baseline(environment, test_name)

    if baseline is None:
        # No baseline exists - record current results
        baseline_manager.save_baseline(
            env=environment,
            test_name=test_name,
            mean_ms=current_mean,
            min_ms=current_min,
            max_ms=current_max,
            query_count=query_count,
        )
        print(f"\n  [BASELINE] Recorded new baseline for {test_name} ({environment.value})")
        print(f"             Mean: {current_mean:.1f}ms")
        return

    # Compare against baseline
    baseline_mean = baseline["mean_ms"]
    recorded_at = baseline.get("recorded_at", "unknown")

    # Parse the date for display
    if recorded_at != "unknown":
        try:
            dt = datetime.fromisoformat(recorded_at)
            recorded_at = dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    diff_ms = current_mean - baseline_mean
    diff_pct = (diff_ms / baseline_mean) * 100 if baseline_mean > 0 else 0

    print(f"\n  [BASELINE] Comparison for {test_name} ({environment.value})")
    print(f"             Baseline mean: {baseline_mean:.1f}ms (recorded {recorded_at})")
    print(f"             Current mean:  {current_mean:.1f}ms")
    print(f"             Difference:    {diff_ms:+.1f}ms ({diff_pct:+.1f}%)")

    if diff_pct > threshold_pct:
        raise PerformanceRegression(
            f"Performance regression detected in {test_name}\n"
            f"  Environment: {environment.value}\n"
            f"  Baseline mean: {baseline_mean:.1f}ms (recorded {recorded_at})\n"
            f"  Current mean:  {current_mean:.1f}ms\n"
            f"  Regression:    {diff_pct:+.1f}% (threshold: {threshold_pct}%)"
        )
