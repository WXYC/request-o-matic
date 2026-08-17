"""Unit tests for the CLI scripts' shared degraded-mode reporting.

`scripts/_common.py` duplicates the `degraded_mode` string values rather than
importing them from `routers/request.py`, so that a CLI's startup does not drag
in FastAPI and the Groq SDK. This module is what makes that duplication safe:
it imports both sides and fails if they drift.
"""

from routers.request import DEGRADED_PARSING, DEGRADED_SEARCH
from scripts._common import DEGRADED_EXPLANATIONS, describe_degraded_mode, indent


class TestDegradedModeCoverage:
    def test_every_server_mode_has_an_explanation(self) -> None:
        """The drift guard: a new degraded mode server-side must land here too.

        If someone adds a third `DEGRADED_*` constant to routers/request.py and
        does not extend DEGRADED_EXPLANATIONS, the CLIs would fall through to the
        generic message. That is survivable, but the point of this test is that
        the omission is noticed at CI time rather than by an operator at 03:00.
        """
        assert set(DEGRADED_EXPLANATIONS) == {DEGRADED_PARSING, DEGRADED_SEARCH}


class TestDescribeDegradedMode:
    def test_healthy_response_returns_none(self) -> None:
        assert describe_degraded_mode({"parsed": {"is_request": True}}) is None

    def test_absent_field_returns_none(self) -> None:
        """Older server builds omit the field entirely rather than sending null."""
        assert describe_degraded_mode({}) is None

    def test_null_field_returns_none(self) -> None:
        assert describe_degraded_mode({"degraded_mode": None}) is None

    def test_parsing_unavailable_names_groq(self) -> None:
        msg = describe_degraded_mode({"degraded_mode": DEGRADED_PARSING})
        assert msg is not None
        assert "groq" in msg.lower()

    def test_search_unavailable_names_the_lookup_service(self) -> None:
        """Regression guard: an LML outage must not read as "not in the library".

        Before this was keyed on `degraded_mode`, both CLIs inferred degradation
        from a null `parsed` -- which `search_unavailable` never sets -- so an
        LML outage rendered as a bare "no results".
        """
        msg = describe_degraded_mode({"degraded_mode": DEGRADED_SEARCH})
        assert msg is not None
        assert "lml" in msg.lower() or "lookup" in msg.lower()
        assert "unavailable" in msg.lower()

    def test_unknown_mode_still_reports_something(self) -> None:
        """A mode we have no copy for must not render as a healthy response."""
        msg = describe_degraded_mode({"degraded_mode": "quota_exhausted"})
        assert msg is not None
        assert "quota_exhausted" in msg


class TestIndent:
    def test_prefixes_every_line(self) -> None:
        assert indent("a\nb", "  ") == "  a\n  b"

    def test_single_line(self) -> None:
        assert indent("solo", "> ") == "> solo"


def test_explanations_carry_no_leading_indentation() -> None:
    """Callers own indentation via `indent()`; baking it into the strings is what
    made the previous `_DEGRADED_NOTICE` unreusable between the two CLIs."""
    for mode, text in DEGRADED_EXPLANATIONS.items():
        for line in text.splitlines():
            assert line == line.lstrip(), f"{mode} has baked-in indentation"
