"""Unit tests for scripts/lookup.py Server-Timing rendering (PR 3b), its
handling of the server's degraded `parsing_unavailable` mode, and its shelf
location rendering."""

import pytest

from routers.request import DEGRADED_PARSING, DEGRADED_SEARCH
from scripts._common import describe_degraded_mode
from scripts.lookup import (
    print_library_results,
    print_parsed_request,
    print_search_summary,
    print_server_timing,
    run_lookup,
)
from services.slack import build_slack_blocks
from tests.factories import make_degraded_response, make_library_item

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

    def test_lml_total_and_new_legs_get_friendly_labels(self, capsys):
        """LML's forwarded self-measured total (renamed lml_total) and the new
        queue_wait / lml_wall / event_loop_lag legs render with friendly
        labels rather than raw snake_case."""
        header = (
            "parse;dur=3, queue_wait;dur=12, library_search;dur=41, "
            "event_loop_lag;dur=3, lml_wall;dur=8600, lml_total;dur=8560, "
            "lookup_service;dur=8610, total;dur=8620"
        )
        print_server_timing(header, round_trip_ms=8990.0)
        out = capsys.readouterr().out
        for label in (
            "LML: total (self-measured)",
            "LML: wall (incl. framework)",
            "LML: queue wait",
            "LML: event-loop lag",
        ):
            assert label in out
        for raw in ("lml_total", "lml_wall", "queue_wait", "event_loop_lag"):
            assert raw not in out

    def test_lml_wall_and_lml_total_print_as_rollups(self, capsys):
        """Roll-ups render in a fixed nesting order (lookup_service, lml_wall,
        lml_total, total), independent of header order. The synthetic header
        below deliberately lists lml_wall/lml_total *before* lookup_service, so
        this also guards that ordering follows ``_ROLLUP_STAGES``, not the
        header — a regression here would otherwise pass on an unrealistic
        header while breaking on real ROM output (lookup_service first)."""
        header = (
            "parse;dur=3, queue_wait;dur=12, library_search;dur=41, "
            "event_loop_lag;dur=3, lml_wall;dur=8600, lml_total;dur=8560, "
            "lookup_service;dur=8610, total;dur=8620"
        )
        print_server_timing(header, round_trip_ms=8990.0)
        lines = capsys.readouterr().out.splitlines()
        idx_leaf = next(i for i, ln in enumerate(lines) if "queue wait" in ln.lower())
        idx_lookup_service_rollup = next(
            i for i, ln in enumerate(lines) if "lml round-trip" in ln.lower()
        )
        idx_lml_wall = next(i for i, ln in enumerate(lines) if "wall (incl" in ln.lower())
        idx_lml_total = next(
            i for i, ln in enumerate(lines) if "total (self-measured)" in ln.lower()
        )
        idx_total = next(i for i, ln in enumerate(lines) if "server total" in ln.lower())
        # Leaves precede every roll-up regardless of header order.
        assert idx_leaf < idx_lookup_service_rollup
        # Roll-ups follow _ROLLUP_STAGES order, NOT header order.
        assert idx_lookup_service_rollup < idx_lml_wall < idx_lml_total < idx_total


class TestDegradedParsingOutput:
    """`parsed: null` is a documented server response, not a client error.

    When Groq is unavailable the service degrades to `parsing_unavailable` and
    returns `{"parsed": null, ...}` (see docs/architecture.md). The CLI used to
    do `data.get("parsed", {})`, which returns None when the key is *present and
    null* rather than absent -- so it died with a bare
    `'NoneType' object has no attribute 'get'` and told the operator nothing.
    That masked a real production incident on 2026-08-17, when Groq
    decommissioned the pinned model and every parse 404'd.
    """

    def test_parsed_none_reports_unavailable_instead_of_raising(self, capsys):
        """A null `parsed` renders an explanatory notice, not an AttributeError."""
        print_parsed_request(None, describe_degraded_mode({"degraded_mode": DEGRADED_PARSING}))
        out = capsys.readouterr().out
        assert "unavailable" in out.lower()

    def test_parsed_none_without_a_mode_still_does_not_raise(self, capsys):
        """Defensive: a null `parsed` with no `degraded_mode` must still render."""
        print_parsed_request(None)
        out = capsys.readouterr().out
        assert "no parse" in out.lower()

    def test_search_summary_tolerates_null_parsed(self, capsys):
        """The search section survives a null `parsed` and reports no search ran."""
        print_search_summary({"parsed": None, "library_results": [], "search_type": "none"})
        out = capsys.readouterr().out
        assert "no library search" in out.lower() or "no parse" in out.lower()

    def test_search_unavailable_is_not_reported_as_no_results(self, capsys):
        """An LML outage must read as degraded, not as "not in the library"."""
        data = make_degraded_response(DEGRADED_SEARCH)
        print_search_summary(data, describe_degraded_mode(data))
        out = capsys.readouterr().out.lower()
        assert "unavailable" in out
        assert "no results" not in out

    @pytest.mark.asyncio
    async def test_run_lookup_completes_on_degraded_response(self, httpx_mock, capsys):
        """End-to-end repro: the exact prod payload must not crash the CLI.

        Regression guard for the crash an operator hit running
        `lookup "vi scose poise, autechre"` against production while the service
        was in `parsing_unavailable`.
        """
        httpx_mock.add_response(json=make_degraded_response())

        data = await run_lookup("vi scose poise, autechre")

        assert data["parsed"] is None
        out = capsys.readouterr().out
        assert "unavailable" in out.lower()


class TestLibraryResultLocation:
    """The printed shelf location must be the one the service composed.

    The CLI used to re-derive `Location:` from `call_letters`,
    `artist_call_number` and `release_call_number`, dropping genre and format.
    Genre is the shelf *section*, so the derived string is far less specific
    than the wire value: over a 64,193-row library.db, 48.8% of rows shared
    their derived locator with another row against 7.3% for the composed one,
    and `V/A 0/3` alone named 57 records across 12 genres (#298). Slack has
    always rendered `item.call_number`, so the two surfaces disagreed about
    where a record physically is.
    """

    def test_location_prints_the_composed_call_number_with_genre_and_format(self, capsys):
        """The wire `call_number` is printed verbatim, genre and format included."""
        item = make_library_item(
            artist="Jessica Pratt",
            title="On Your Own Love Again",
            genre="Rock",
            format="CD",
            call_letters="PRA",
            artist_call_number=4,
            release_call_number=2,
        )

        print_library_results([item.model_dump(mode="json")], None)

        out = capsys.readouterr().out
        assert "Location: Rock CD PRA 4/2" in out

    @pytest.mark.parametrize(
        ("components", "expected"),
        [
            pytest.param(
                {"call_letters": "CAT", "artist_call_number": 7, "release_call_number": 1},
                "CAT 7/1",
                id="both-components-present",
            ),
            pytest.param(
                {"call_letters": "V/A", "artist_call_number": 0, "release_call_number": 3},
                "V/A 0/3",
                id="zero-artist-number-is-a-value-not-a-blank",
            ),
            pytest.param(
                {"call_letters": "CAT", "artist_call_number": None, "release_call_number": 1},
                "CAT /1",
                id="null-component-renders-blank-not-the-word-None",
            ),
            pytest.param(
                {"call_letters": "CAT", "artist_call_number": 7, "release_call_number": 0},
                "CAT 7/0",
                id="zero-release-number-is-a-value-not-a-blank",
            ),
            pytest.param(
                {"call_letters": "CAT", "artist_call_number": 7, "release_call_number": None},
                "CAT 7/",
                id="null-release-number-renders-blank-not-the-word-None",
            ),
        ],
    )
    def test_location_falls_back_to_components_when_call_number_absent(
        self, capsys, components, expected
    ):
        """A deployed service predating the field still yields a usable locator.

        `call_number` is required by the contract, but the CLI points at
        whatever is deployed, so an absent key falls back to the old derivation
        rather than printing nothing.

        The components are `int | None` on the contract, and the two nullish
        shapes are not interchangeable: `artist_call_number` is `0` for every
        Various-Artists row, so a truthiness test would blank a real value,
        while a null component must not reach the operator as the word `None`.
        """
        item = {"artist": "Cat Power", "title": "Moon Pix", **components}

        print_library_results([item], None)

        # Exact, not containment: "Location: CAT 7/" is a prefix of
        # "Location: CAT 7/None", so `in` cannot catch the word leaking.
        location_line = next(
            line for line in capsys.readouterr().out.splitlines() if "Location:" in line
        )
        assert location_line.strip() == f"Location: {expected}"

    @pytest.mark.parametrize(
        "item",
        [
            pytest.param(
                {"artist": "Juana Molina", "title": "DOGA", "call_number": ""},
                id="empty-call-number",
            ),
            pytest.param(
                {"artist": "Juana Molina", "title": "DOGA"},
                id="no-call-number-and-no-components",
            ),
            pytest.param(
                {
                    "artist": "Juana Molina",
                    "title": "DOGA",
                    "call_letters": "",
                    "artist_call_number": 7,
                    "release_call_number": 1,
                },
                id="empty-call-letters-do-not-print-a-letterless-locator",
            ),
            pytest.param(
                {
                    "artist": "Juana Molina",
                    "title": "DOGA",
                    "call_letters": "MOL",
                    "artist_call_number": None,
                    "release_call_number": None,
                },
                id="letters-with-no-numbers-do-not-print-a-bare-slash",
            ),
            pytest.param(
                {
                    "artist": "Juana Molina",
                    "title": "DOGA",
                    "call_number": "",
                    "call_letters": "MOL",
                    "artist_call_number": 0,
                    "release_call_number": 3,
                },
                id="empty-call-number-does-not-resurrect-the-derivation",
            ),
            pytest.param(
                {
                    "artist": "Juana Molina",
                    "title": "DOGA",
                    "call_number": None,
                    "call_letters": "MOL",
                    "artist_call_number": 0,
                    "release_call_number": 3,
                },
                id="null-call-number-does-not-resurrect-the-derivation",
            ),
        ],
    )
    def test_empty_or_missing_call_number_prints_none(self, capsys, item):
        """An empty locator reads as `(none)`, never as a bare `/` or a traceback."""
        print_library_results([item], None)

        out = capsys.readouterr().out
        assert "Location: (none)" in out
        location_line = next(line for line in out.splitlines() if "Location:" in line)
        assert "/" not in location_line

    def test_cli_and_slack_render_the_same_locator(self, capsys):
        """Both surfaces print one string for one item, pinned over a shared fixture."""
        item = make_library_item(
            artist="Duke Ellington & John Coltrane",
            title="Duke Ellington & John Coltrane",
            genre="Jazz",
            format="CD",
            call_letters="ELL",
            artist_call_number=12,
            release_call_number=3,
        )

        print_library_results([item.model_dump(mode="json")], None)
        cli_line = next(
            line for line in capsys.readouterr().out.splitlines() if "Location:" in line
        )
        cli_locator = cli_line.split("Location:", 1)[1].strip()

        # build_slack_blocks emits [header, item]; the item block's text is
        # `*artist*` / title / `_call_number_`. Index the known slot rather
        # than scanning for underscores, which a title like `_o_` would match.
        blocks = build_slack_blocks("Now playing", [(item, None)])
        slack_line = blocks[1]["text"]["text"].splitlines()[2]
        slack_locator = slack_line.removeprefix("_").removesuffix("_")

        assert cli_locator == slack_locator == item.call_number
