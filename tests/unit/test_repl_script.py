"""Unit tests for scripts/repl.py output rendering.

Companion to tests/unit/test_lookup_script.py. Both scripts are documented
operator tools (docs/scripts.md) and both read the same `/request` response, so
both have to survive the server's degraded `parsing_unavailable` shape.
"""

from routers.request import DEGRADED_SEARCH
from scripts.repl import print_result
from tests.factories import make_degraded_response


class TestDegradedParsingOutput:
    def test_null_parsed_reports_unavailable_instead_of_raising(self, capsys):
        """`parsed: null` must render a diagnosis, not an AttributeError.

        `data.get("parsed", {})` returns None for a present-but-null key rather
        than the `{}` default, so the subsequent `.get` blew up the REPL. An
        operator sitting in the REPL is the most likely person to be present
        when parsing goes degraded, so this is exactly where a clear message
        matters most.
        """
        print_result(make_degraded_response())
        out = capsys.readouterr().out
        assert "unavailable" in out.lower()

    def test_degraded_output_does_not_claim_a_parse(self, capsys):
        """The degraded notice must not print empty parse slots alongside itself.

        `Is Request: None` / `Type: None` reads as "parsed, found nothing",
        which is a different and misleading diagnosis.
        """
        print_result(make_degraded_response())
        out = capsys.readouterr().out
        assert "Is Request:" not in out

    def test_normal_response_still_renders_parse_fields(self, capsys):
        """Regression guard: the degraded branch must not swallow the happy path."""
        print_result(
            {
                "parsed": {
                    "is_request": True,
                    "message_type": "request",
                    "artist": "Juana Molina",
                    "song": "la paradoja",
                    "album": "DOGA",
                },
                "library_results": [],
                "artwork": None,
            }
        )
        out = capsys.readouterr().out
        assert "Juana Molina" in out
        assert "la paradoja" in out

    def test_search_unavailable_is_not_reported_as_no_results(self, capsys):
        """An LML outage must not render as "not in the library".

        `search_unavailable` leaves `parsed` populated and `library_results`
        empty, so inferring degradation from a null `parsed` misses it entirely
        and the operator is told the record isn't in the catalog.
        """
        print_result(make_degraded_response(DEGRADED_SEARCH))
        out = capsys.readouterr().out.lower()
        assert "unavailable" in out
        assert "no results found." not in out
