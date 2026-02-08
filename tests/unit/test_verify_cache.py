"""Tests for verify_cache multi-index matching pipeline."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Load verify_cache module from the hyphenated directory path
_SCRIPT_PATH = (
    Path(__file__).parent.parent.parent / "scripts" / "setup-discogs-db" / "verify_cache.py"
)
_spec = importlib.util.spec_from_file_location("verify_cache", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
_vc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_vc)

# Re-export for cleaner access in tests
normalize_title = _vc.normalize_title
normalize_artist = _vc.normalize_artist
LibraryIndex = _vc.LibraryIndex
score_exact = _vc.score_exact
score_token_set = _vc.score_token_set
score_token_sort = _vc.score_token_sort
score_two_stage = _vc.score_two_stage
MultiIndexMatcher = _vc.MultiIndexMatcher
Decision = _vc.Decision
load_artist_mappings = _vc.load_artist_mappings
save_artist_mappings = _vc.save_artist_mappings
classify_compilation = _vc.classify_compilation
load_discogs_releases = _vc.load_discogs_releases

# ---------------------------------------------------------------------------
# Step 1: Normalization
# ---------------------------------------------------------------------------


class TestNormalizeTitle:
    """Test album/title normalization for matching."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ('Automanikk 12"', "automanikk"),
            ("Cobra (2 cd set)", "cobra"),
            ("OK Computer (reissue)", "ok computer"),
            ("Dummy (3)", "dummy"),  # Discogs disambiguation
            ("Abbey Road", "abbey road"),  # no-op
            ("In Utero (ep)", "in utero"),
            ("Loveless (deluxe edition)", "loveless"),
            ("  Spaced Out  ", "spaced out"),
            ('Raw Power 7"', "raw power"),
            ("Homogenic (2lp)", "homogenic"),
        ],
        ids=[
            "vinyl_12_inch",
            "2_cd_set",
            "reissue",
            "discogs_disambiguation",
            "no_op",
            "ep_suffix",
            "deluxe_edition",
            "whitespace",
            "7_inch",
            "2lp",
        ],
    )
    def test_normalize_title(self, raw, expected):
        assert normalize_title(raw) == expected


class TestNormalizeArtist:
    """Test artist name normalization."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Radiohead", "radiohead"),
            ("Beatles, The", "the beatles"),
            ("Bjork (2)", "bjork"),  # Discogs disambiguation
            ("Artist [Scotland]", "artist"),  # Library disambiguation
            ("Simon & Garfunkel", "simon and garfunkel"),
            ("Simon And Garfunkel", "simon and garfunkel"),
            ("Guns N' Roses", "guns n roses"),
            ("  Spaced  ", "spaced"),
            ("Björk", "bjork"),  # Accent stripping
            ("E.S.T.", "e.s.t."),  # Dots preserved
        ],
        ids=[
            "simple",
            "comma_the",
            "discogs_disambiguation",
            "library_disambiguation",
            "ampersand_to_and",
            "and_normalized",
            "apostrophe",
            "whitespace",
            "accents",
            "dots_preserved",
        ],
    )
    def test_normalize_artist(self, raw, expected):
        assert normalize_artist(raw) == expected


# ---------------------------------------------------------------------------
# Step 2: LibraryIndex
# ---------------------------------------------------------------------------

# Shared fixture: a small hand-crafted library for testing
SAMPLE_LIBRARY_ROWS = [
    ("Radiohead", "OK Computer"),
    ("Radiohead", "Kid A"),
    ("Joy Division", "Unknown Pleasures"),
    ("Joy Division", "Closer"),
    ("Aphex Twin", "Selected Ambient Works 85-92"),
    ("Aphex Twin", 'Analord 10 12"'),
    ("Beatles, The", "Abbey Road"),
    ("Simon & Garfunkel", "Bridge Over Troubled Water"),
    ("Björk", "Homogenic"),
    ("Various Artists - Compilations", "Sugar Hill"),
    ("Soundtracks - S", "Lost In Translation"),
]


@pytest.fixture
def sample_index():
    """Build a LibraryIndex from hand-crafted rows."""
    return LibraryIndex.from_rows(SAMPLE_LIBRARY_ROWS)


class TestLibraryIndex:
    """Test LibraryIndex construction and data structures."""

    def test_exact_pairs_populated(self, sample_index):
        """Normalized (artist, title) tuples are in exact_pairs."""
        assert ("radiohead", "ok computer") in sample_index.exact_pairs
        assert ("radiohead", "kid a") in sample_index.exact_pairs

    def test_exact_pairs_normalizes_titles(self, sample_index):
        """Title normalization strips suffixes before inserting into exact_pairs."""
        # 'Analord 10 12"' should become 'analord 10'
        assert ("aphex twin", "analord 10") in sample_index.exact_pairs

    def test_exact_pairs_normalizes_artists(self, sample_index):
        """Artist normalization handles ampersands and accents."""
        assert ("simon and garfunkel", "bridge over troubled water") in sample_index.exact_pairs
        assert ("bjork", "homogenic") in sample_index.exact_pairs

    def test_artist_to_titles_mapping(self, sample_index):
        """Each artist maps to the set of their normalized titles."""
        assert sample_index.artist_to_titles["radiohead"] == {"ok computer", "kid a"}
        assert "unknown pleasures" in sample_index.artist_to_titles["joy division"]

    def test_combined_strings_format(self, sample_index):
        """Combined strings use 'artist ||| title' format."""
        assert "radiohead ||| ok computer" in sample_index.combined_strings

    def test_combined_to_original_maps_back(self, sample_index):
        """combined_to_original maps the combined string back to the normalized pair."""
        key = "radiohead ||| ok computer"
        assert sample_index.combined_to_original[key] == ("radiohead", "ok computer")

    def test_all_artists_populated(self, sample_index):
        """all_artists contains deduplicated normalized artist names."""
        artists = sample_index.all_artists
        assert "radiohead" in artists
        assert "joy division" in artists
        # No duplicates
        assert len(set(artists)) == len(artists)

    def test_various_artists_excluded(self, sample_index):
        """Compilation entries are separated into compilation_index."""
        # "Various Artists - Compilations" and "Soundtracks - S" are compilations
        assert "various artists - compilations" not in sample_index.all_artists
        assert "soundtracks - s" not in sample_index.all_artists
        # But they should be in compilation_titles
        assert sample_index.compilation_titles is not None
        assert "sugar hill" in sample_index.compilation_titles

    def test_deduplication(self):
        """Duplicate rows produce unique entries."""
        rows = [
            ("Radiohead", "OK Computer"),
            ("Radiohead", "OK Computer"),  # duplicate
        ]
        idx = LibraryIndex.from_rows(rows)
        assert len(idx.exact_pairs) == 1
        assert len(idx.combined_strings) == 1


# ---------------------------------------------------------------------------
# Step 3: Individual Scorers
# ---------------------------------------------------------------------------


class TestScoreExact:
    """Test exact pair matching scorer."""

    def test_exact_match_returns_1(self, sample_index):
        assert score_exact("radiohead", "ok computer", sample_index) == 1.0

    def test_no_match_returns_0(self, sample_index):
        assert score_exact("radiohead", "the bends", sample_index) == 0.0

    def test_normalizes_before_lookup(self, sample_index):
        """Inputs are already normalized by caller, but verify exact lookup."""
        assert score_exact("the beatles", "abbey road", sample_index) == 1.0


class TestScoreTokenSet:
    """Test token_set_ratio on combined 'artist ||| title' strings."""

    def test_high_similarity_for_exact_match(self, sample_index):
        score = score_token_set("radiohead", "ok computer", sample_index)
        assert score >= 0.95

    def test_partial_artist_with_matching_title_is_high(self, sample_index):
        """'Joy' / 'Unknown Pleasures' vs 'Joy Division' / 'Unknown Pleasures'.

        token_set_ratio is generous with subset matches -- 'joy' is a subset
        of 'joy division' tokens, so this scores very high (1.0). This is the
        known weakness that multi-index agreement compensates for.
        """
        score = score_token_set("joy", "unknown pleasures", sample_index)
        assert score >= 0.9  # token_set_ratio treats subsets generously

    def test_no_match_is_low(self, sample_index):
        score = score_token_set("zzyzx nonexistent", "fake album", sample_index)
        assert score < 0.5


class TestScoreTokenSort:
    """Test token_sort_ratio on combined strings."""

    def test_high_similarity_for_exact_match(self, sample_index):
        score = score_token_sort("radiohead", "ok computer", sample_index)
        assert score >= 0.95

    def test_no_match_is_low(self, sample_index):
        score = score_token_sort("zzyzx nonexistent", "fake album", sample_index)
        assert score < 0.5


class TestScoreTwoStage:
    """Test two-stage scorer: artist match then title match."""

    def test_exact_artist_and_title(self, sample_index):
        score = score_two_stage("radiohead", "ok computer", sample_index)
        assert score >= 0.95

    def test_penalizes_short_artist_with_wrong_title(self, sample_index):
        """'Joy' fuzzy-matches 'Joy Division' but 'Some Album' doesn't match titles.

        The two-stage scorer uses geometric mean: even if artist matches well,
        a poor title match drags the score down. Should be moderate at best.
        """
        score = score_two_stage("joy", "some album", sample_index)
        assert score < 0.75  # below the KEEP threshold

    def test_est_matches_est(self):
        """E.S.T. should match e.s.t. since dots are preserved."""
        rows = [("E.S.T.", "Tuesday Wonderland")]
        idx = LibraryIndex.from_rows(rows)
        score = score_two_stage("e.s.t.", "tuesday wonderland", idx)
        assert score >= 0.95

    def test_no_artist_match_returns_0(self, sample_index):
        score = score_two_stage("zzyzx nonexistent", "fake album", sample_index)
        assert score == 0.0


# ---------------------------------------------------------------------------
# Step 4: Multi-Index Agreement
# ---------------------------------------------------------------------------

# Module path for patching scorers
_MODULE = "verify_cache"


class TestMultiIndexMatcher:
    """Test MultiIndexMatcher.classify() with mocked scorer outputs."""

    def test_exact_match_is_keep(self, sample_index):
        """An exact pair match should always be KEEP."""
        matcher = MultiIndexMatcher(sample_index)
        result = matcher.classify("radiohead", "ok computer")
        assert result.decision == Decision.KEEP

    def test_two_of_three_above_threshold_is_keep(self, sample_index):
        """When 2 of 3 fuzzy scorers are above 0.75 (including two-stage), result is KEEP."""
        matcher = MultiIndexMatcher(sample_index)
        with (
            patch.object(_vc, "score_exact", return_value=0.0),
            patch.object(_vc, "score_token_set", return_value=0.80),
            patch.object(_vc, "score_token_sort", return_value=0.50),
            patch.object(_vc, "score_two_stage", return_value=0.78),
        ):
            result = matcher.classify("some artist", "some title")
        assert result.decision == Decision.KEEP

    def test_two_of_three_without_two_stage_is_not_keep(self, sample_index):
        """When 2 of 3 are above 0.75 but two-stage is low, NOT KEEP.

        This prevents false positives from subset matching on short names.
        """
        matcher = MultiIndexMatcher(sample_index)
        with (
            patch.object(_vc, "score_exact", return_value=0.0),
            patch.object(_vc, "score_token_set", return_value=0.80),
            patch.object(_vc, "score_token_sort", return_value=0.78),
            patch.object(_vc, "score_two_stage", return_value=0.50),
        ):
            result = matcher.classify("some artist", "some title")
        assert result.decision != Decision.KEEP

    def test_one_high_plus_one_moderate_is_keep(self, sample_index):
        """One scorer >= 0.85 plus another >= 0.70 (including two-stage) is KEEP."""
        matcher = MultiIndexMatcher(sample_index)
        with (
            patch.object(_vc, "score_exact", return_value=0.0),
            patch.object(_vc, "score_token_set", return_value=0.90),
            patch.object(_vc, "score_token_sort", return_value=0.40),
            patch.object(_vc, "score_two_stage", return_value=0.72),
        ):
            result = matcher.classify("some artist", "some title")
        assert result.decision == Decision.KEEP

    def test_all_below_near_miss_is_prune(self, sample_index):
        """When all scorers are below the REVIEW threshold, result is PRUNE."""
        matcher = MultiIndexMatcher(sample_index)
        with (
            patch.object(_vc, "score_exact", return_value=0.0),
            patch.object(_vc, "score_token_set", return_value=0.30),
            patch.object(_vc, "score_token_sort", return_value=0.25),
            patch.object(_vc, "score_two_stage", return_value=0.20),
        ):
            result = matcher.classify("zzyzx", "fake album")
        assert result.decision == Decision.PRUNE

    def test_near_miss_range_is_review(self, sample_index):
        """When max score is in the review range (0.65-0.75), result is REVIEW."""
        matcher = MultiIndexMatcher(sample_index)
        with (
            patch.object(_vc, "score_exact", return_value=0.0),
            patch.object(_vc, "score_token_set", return_value=0.70),
            patch.object(_vc, "score_token_sort", return_value=0.60),
            patch.object(_vc, "score_two_stage", return_value=0.55),
        ):
            result = matcher.classify("some artist", "some title")
        assert result.decision == Decision.REVIEW

    def test_joy_does_not_keep_as_joy_division(self, sample_index):
        """'Joy' / 'Random Album' should not match 'Joy Division' and KEEP.

        Even though token_set_ratio might be generous, the multi-index
        agreement should not let it through without title confirmation.
        """
        matcher = MultiIndexMatcher(sample_index)
        result = matcher.classify("joy", "random album")
        assert result.decision != Decision.KEEP

    def test_result_contains_scores(self, sample_index):
        """MatchResult should contain individual scorer scores."""
        matcher = MultiIndexMatcher(sample_index)
        result = matcher.classify("radiohead", "ok computer")
        assert result.exact_score == 1.0
        assert hasattr(result, "token_set_score")
        assert hasattr(result, "token_sort_score")
        assert hasattr(result, "two_stage_score")


# ---------------------------------------------------------------------------
# Step 5: Artist Mappings Persistence
# ---------------------------------------------------------------------------


class TestArtistMappings:
    """Test loading and saving artist_mappings.json."""

    def test_load_empty_mappings(self, tmp_path):
        """Missing file returns empty keep/prune dicts."""
        mappings = load_artist_mappings(tmp_path / "nonexistent.json")
        assert mappings == {"keep": {}, "prune": {}}

    def test_load_existing_mappings(self, tmp_path):
        """Reads JSON and returns keep/prune dicts."""
        path = tmp_path / "mappings.json"
        data = {
            "keep": {"bjork (2)": "Bjork", "sunn o)))": "Sunn O)))"},
            "prune": {"joy": None},
        }
        path.write_text(json.dumps(data))
        mappings = load_artist_mappings(path)
        assert mappings["keep"]["bjork (2)"] == "Bjork"
        assert mappings["prune"]["joy"] is None

    def test_save_mappings(self, tmp_path):
        """Writes JSON that round-trips correctly."""
        path = tmp_path / "mappings.json"
        data = {
            "keep": {"bjork (2)": "Bjork"},
            "prune": {"joy": None},
        }
        save_artist_mappings(path, data)
        loaded = load_artist_mappings(path)
        assert loaded == data

    def test_mappings_override_keep(self, sample_index):
        """A REVIEW release whose artist is in keep mappings -> KEEP."""
        mappings = {"keep": {"some artist": "Some Artist"}, "prune": {}}
        matcher = MultiIndexMatcher(sample_index, artist_mappings=mappings)
        # This artist wouldn't normally be found in the index
        with (
            patch.object(_vc, "score_exact", return_value=0.0),
            patch.object(_vc, "score_token_set", return_value=0.70),
            patch.object(_vc, "score_token_sort", return_value=0.60),
            patch.object(_vc, "score_two_stage", return_value=0.55),
        ):
            result = matcher.classify("some artist", "some title")
        assert result.decision == Decision.KEEP

    def test_mappings_override_prune(self, sample_index):
        """A release whose artist is in prune mappings -> PRUNE."""
        mappings = {"keep": {}, "prune": {"joy": None}}
        matcher = MultiIndexMatcher(sample_index, artist_mappings=mappings)
        result = matcher.classify("joy", "unknown pleasures")
        assert result.decision == Decision.PRUNE


# ---------------------------------------------------------------------------
# Step 6: Compilation Handling
# ---------------------------------------------------------------------------


class TestCompilationHandling:
    """Test compilation classification by title-only matching."""

    def test_compilation_matched_by_title(self, sample_index):
        """'Various Artists' / 'Sugar Hill' matches the compilation title."""
        result = classify_compilation("sugar hill", sample_index)
        assert result == Decision.KEEP

    def test_compilation_no_match(self, sample_index):
        """Unknown compilation title -> PRUNE."""
        result = classify_compilation("unknown comp", sample_index)
        assert result == Decision.PRUNE

    def test_compilation_fuzzy_match(self):
        """Fuzzy title matching for compilations (minor spelling differences)."""
        rows = [("Various Artists - Compilations", "Lost In Translation")]
        idx = LibraryIndex.from_rows(rows)
        result = classify_compilation("lost in translation", idx)
        assert result == Decision.KEEP


# ---------------------------------------------------------------------------
# Step 7: Discogs Data Loading
# ---------------------------------------------------------------------------


class TestLoadDiscogsReleases:
    """Test loading releases from PostgreSQL with mocked asyncpg."""

    @pytest.mark.asyncio
    async def test_returns_release_tuples(self):
        """Returns list of (release_id, artist_name, title) tuples."""
        mock_conn = MagicMock()
        mock_conn.fetch = AsyncMock(
            return_value=[
                {"id": 28138, "title": "Confield", "artist_name": "Autechre"},
                {"id": 12345, "title": "OK Computer", "artist_name": "Radiohead"},
            ]
        )
        releases = await load_discogs_releases(mock_conn)
        assert len(releases) == 2
        assert releases[0] == (28138, "Autechre", "Confield")
        assert releases[1] == (12345, "Radiohead", "OK Computer")

    @pytest.mark.asyncio
    async def test_query_filters_extra_artists(self):
        """Query should only include main artists (extra = 0)."""
        mock_conn = MagicMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        await load_discogs_releases(mock_conn)
        call_args = mock_conn.fetch.call_args
        query = call_args[0][0]
        assert "extra = 0" in query or "extra=0" in query

    @pytest.mark.asyncio
    async def test_empty_results(self):
        """Returns empty list when no releases found."""
        mock_conn = MagicMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        releases = await load_discogs_releases(mock_conn)
        assert releases == []
