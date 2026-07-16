"""Unit tests for core/server_timing.py (Server-Timing header parsing)."""

from core.server_timing import parse_server_timing


class TestParseServerTiming:
    """Tests for parse_server_timing()."""

    def test_basic_two_entries(self):
        """A well-formed two-entry header parses to ordered (name, dur) pairs."""
        assert parse_server_timing("parse;dur=12.3, total;dur=45.6") == [
            ("parse", 12.3),
            ("total", 45.6),
        ]

    def test_none_returns_empty(self):
        """A missing header (None) yields an empty list, never raises."""
        assert parse_server_timing(None) == []

    def test_empty_string_returns_empty(self):
        """An empty string yields an empty list."""
        assert parse_server_timing("") == []

    def test_whitespace_only_returns_empty(self):
        """A whitespace-only header yields an empty list."""
        assert parse_server_timing("   ") == []

    def test_no_space_after_comma(self):
        """Entries separated by a bare comma (no space) still parse."""
        assert parse_server_timing("parse;dur=12.3,total;dur=45.6") == [
            ("parse", 12.3),
            ("total", 45.6),
        ]

    def test_integer_dur_becomes_float(self):
        """An integer-valued dur is returned as a float."""
        assert parse_server_timing("discogs;dur=806") == [("discogs", 806.0)]

    def test_zero_dur_is_kept(self):
        """dur=0 is a real measurement, not a missing one — it must be kept."""
        assert parse_server_timing("discogs;dur=0") == [("discogs", 0.0)]

    def test_desc_param_is_ignored(self):
        """A trailing desc parameter is ignored; only dur is extracted."""
        assert parse_server_timing('cache;dur=53.2;desc="Cache Read"') == [("cache", 53.2)]

    def test_desc_before_dur(self):
        """dur is found regardless of parameter order within an entry."""
        assert parse_server_timing("cpu;desc=compute;dur=2.1") == [("cpu", 2.1)]

    def test_name_only_entry_is_skipped(self):
        """An entry with no dur param cannot merge into as_server_timing(extra),
        so it is skipped rather than raising or defaulting to 0."""
        assert parse_server_timing("missedCache, parse;dur=1") == [("parse", 1.0)]

    def test_non_numeric_dur_is_skipped(self):
        """A malformed (non-numeric) dur skips only that entry, not the whole header."""
        assert parse_server_timing("parse;dur=abc, total;dur=5") == [("total", 5.0)]

    def test_order_and_duplicates_preserved(self):
        """The return is an ordered list (not a dict): order and duplicate names survive."""
        assert parse_server_timing("a;dur=1, b;dur=2, a;dur=3") == [
            ("a", 1.0),
            ("b", 2.0),
            ("a", 3.0),
        ]

    def test_trailing_comma_and_blanks_skipped(self):
        """Empty entries from stray/trailing commas are skipped."""
        assert parse_server_timing("parse;dur=1, , total;dur=2,") == [
            ("parse", 1.0),
            ("total", 2.0),
        ]

    def test_realistic_lml_header(self):
        """A realistic forwarded LML header parses in full."""
        header = (
            "library_search;dur=41.2, metadata_enrichment;dur=8500.7, "
            "discogs;dur=806, total;dur=8560.1"
        )
        assert parse_server_timing(header) == [
            ("library_search", 41.2),
            ("metadata_enrichment", 8500.7),
            ("discogs", 806.0),
            ("total", 8560.1),
        ]

    def test_non_finite_dur_is_skipped(self):
        """``float()`` accepts 'inf'/'nan', but they are not valid Server-Timing
        durations — emitting ``dur=inf`` would break a strict downstream parser,
        so they are skipped like any other malformed dur."""
        assert parse_server_timing("a;dur=inf, b;dur=nan, c;dur=5") == [("c", 5.0)]

    def test_negative_dur_is_skipped(self):
        """A negative dur is not a valid measurement; skip it, keep the rest."""
        assert parse_server_timing("a;dur=-3, b;dur=5") == [("b", 5.0)]

    def test_name_with_interior_whitespace_is_skipped(self):
        """A name that is not a valid RFC token (e.g. contains a space) cannot be
        emitted verbatim into a Server-Timing header, so the entry is skipped."""
        assert parse_server_timing("cache hit;dur=5, discogs;dur=10") == [("discogs", 10.0)]

    def test_name_with_control_chars_is_skipped(self):
        """A CR/LF-bearing name (header-injection shaped) is skipped rather than
        copied into rom's response header — latin-1 encodes CR/LF, so letting it
        through would fail at the ASGI send layer, outside the caller's guard."""
        injected = "foo\r\nX-Evil: bar;dur=1, discogs;dur=10"
        assert parse_server_timing(injected) == [("discogs", 10.0)]

    def test_rfc_token_names_are_kept(self):
        """Valid RFC 7230 token names (letters, digits, and tchars) survive the
        token filter — the guard rejects only genuinely non-token names."""
        assert parse_server_timing("db.query_1;dur=2, cache-hit;dur=3") == [
            ("db.query_1", 2.0),
            ("cache-hit", 3.0),
        ]
