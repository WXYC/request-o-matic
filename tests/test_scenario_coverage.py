"""Meta-tests that verify every search scenario has both unit and integration coverage.

These tests fail fast when a new scenario is added to tests/scenarios.py without
corresponding test entries in UNIT_COVERAGE or INTEGRATION_COVERAGE, making
coverage gaps visible in CI.
"""

from tests.scenarios import INTEGRATION_COVERAGE, SCENARIOS, UNIT_COVERAGE


def test_all_scenarios_have_unit_coverage():
    """Every registered scenario must have an entry in UNIT_COVERAGE."""
    missing = set(SCENARIOS) - set(UNIT_COVERAGE)
    assert not missing, f"Scenarios missing from UNIT_COVERAGE: {missing}"


def test_all_scenarios_have_integration_coverage():
    """Every registered scenario must have an entry in INTEGRATION_COVERAGE."""
    missing = set(SCENARIOS) - set(INTEGRATION_COVERAGE)
    assert not missing, f"Scenarios missing from INTEGRATION_COVERAGE: {missing}"


def test_no_orphaned_unit_coverage_entries():
    """UNIT_COVERAGE should not reference scenario IDs that don't exist."""
    orphaned = set(UNIT_COVERAGE) - set(SCENARIOS)
    assert not orphaned, f"UNIT_COVERAGE references unknown scenarios: {orphaned}"


def test_no_orphaned_integration_coverage_entries():
    """INTEGRATION_COVERAGE should not reference scenario IDs that don't exist."""
    orphaned = set(INTEGRATION_COVERAGE) - set(SCENARIOS)
    assert not orphaned, f"INTEGRATION_COVERAGE references unknown scenarios: {orphaned}"


def test_coverage_gaps_are_documented():
    """Scenarios with empty coverage lists are explicitly acknowledged gaps.

    This test prints the current gap summary for visibility but does not fail.
    Coverage gaps are acceptable as long as they are explicitly registered
    (empty list vs missing key).
    """
    unit_gaps = [sid for sid, tests in UNIT_COVERAGE.items() if not tests]
    integration_gaps = [sid for sid, tests in INTEGRATION_COVERAGE.items() if not tests]

    if unit_gaps:
        print(f"\nUnit test gaps ({len(unit_gaps)}): {', '.join(sorted(unit_gaps))}")
    if integration_gaps:
        print(
            f"\nIntegration test gaps ({len(integration_gaps)}): "
            f"{', '.join(sorted(integration_gaps))}"
        )
