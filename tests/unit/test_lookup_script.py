"""Unit tests for scripts/lookup.py Server-Timing rendering (PR 3b)."""

from scripts.lookup import print_server_timing

# A realistic merged header as ROM emits it: rom stages (parse, lookup_service,
# slack_post) + forwarded LML sub-stages (library_search, metadata_enrichment,
# discogs) + rom's single total.
MERGED_HEADER = (
    "parse;dur=3.1, lookup_service;dur=8560, slack_post;dur=42, "
    "library_search;dur=41, metadata_enrichment;dur=8500, discogs;dur=806, total;dur=8610"
)


class TestPrintServerTiming:
    def test_all_stage_durations_rendered(self, capsys):
        """Every leg's duration surfaces, plus the client round-trip."""
        print_server_timing(MERGED_HEADER, round_trip_ms=8990.0)
        out = capsys.readouterr().out
        # the 8.5s metadata_enrichment culprit is visible, with its friendly label
        assert "metadata enrichment" in out.lower()
        assert "8500" in out
        # server total and the client-measured round-trip both surface
        assert "8610" in out
        assert "8990" in out
        assert "round-trip" in out.lower()

    def test_client_round_trip_printed_last(self, capsys):
        """Leaves + roll-ups print first; the client round-trip line is last."""
        print_server_timing(MERGED_HEADER, round_trip_ms=8990.0)
        timing_lines = [ln for ln in capsys.readouterr().out.splitlines() if "ms" in ln]
        assert timing_lines, "expected at least one timing line"
        assert "8990" in timing_lines[-1]
        assert "client" in timing_lines[-1].lower()

    def test_leaf_before_rollup_ordering(self, capsys):
        """A forwarded LML sub-stage (leaf) prints before the lookup_service roll-up."""
        print_server_timing(MERGED_HEADER, round_trip_ms=8990.0)
        lines = capsys.readouterr().out.splitlines()
        idx_enrich = next(i for i, ln in enumerate(lines) if "metadata enrichment" in ln.lower())
        idx_rollup = next(i for i, ln in enumerate(lines) if "lml round-trip" in ln.lower())
        assert idx_enrich < idx_rollup

    def test_absent_header_prints_round_trip_only(self, capsys):
        """No server header (older ROM / flag off) → round-trip plus an explanatory note."""
        print_server_timing(None, round_trip_ms=512.0)
        out = capsys.readouterr().out
        assert "512" in out
        assert "round-trip" in out.lower()
        assert "no server-timing" in out.lower()
        # no per-stage lines when the header is absent
        assert "metadata enrichment" not in out.lower()

    def test_unknown_stage_falls_through_to_raw_name(self, capsys):
        """An unmapped stage name is printed verbatim rather than dropped."""
        print_server_timing("mystery_stage;dur=12, total;dur=20", round_trip_ms=25.0)
        out = capsys.readouterr().out
        assert "mystery_stage" in out
        assert "12" in out

    def test_present_but_unparseable_header_is_noted_as_such(self, capsys):
        """A present-but-corrupt header (every entry rejected) is reported as
        unparseable, not as absent — the two situations mean different things to
        someone debugging why the trace is empty."""
        print_server_timing("no_dur_here, another_no_dur", round_trip_ms=10.0)
        out = capsys.readouterr().out
        assert "unparseable" in out.lower()
        assert "10" in out

    def test_all_forwarded_lml_substages_get_friendly_labels(self, capsys):
        """Every sub-stage LML forwards renders with an ``LML:`` provenance label,
        not its raw snake_case name. Regression guard: the map originally covered
        only 3 of LML's 7 forwarded stages, so album_lookup / track_validation /
        artwork_fetch / identity_resolution leaked through raw (confirmed against
        staging on 2026-07-16)."""
        header = (
            "parse;dur=5, album_lookup;dur=1, library_search;dur=2, "
            "track_validation;dur=1, artwork_fetch;dur=3, metadata_enrichment;dur=4, "
            "identity_resolution;dur=2, discogs;dur=6, lookup_service;dur=20, total;dur=25"
        )
        print_server_timing(header, round_trip_ms=30.0)
        out = capsys.readouterr().out
        for label in (
            "LML: album lookup",
            "LML: track validation",
            "LML: artwork fetch",
            "LML: identity resolution",
        ):
            assert label in out
        # the raw snake_case forms must not leak once the stage is mapped
        for raw in ("album_lookup", "track_validation", "artwork_fetch", "identity_resolution"):
            assert raw not in out
