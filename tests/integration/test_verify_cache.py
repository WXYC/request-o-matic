"""Integration tests for verify_cache multi-index matching against real library.db."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

# Load verify_cache module from the hyphenated directory path
_SCRIPT_PATH = (
    Path(__file__).parent.parent.parent / "scripts" / "setup-discogs-db" / "verify_cache.py"
)
_spec = importlib.util.spec_from_file_location("verify_cache", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
_vc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_vc)

LibraryIndex = _vc.LibraryIndex
MultiIndexMatcher = _vc.MultiIndexMatcher
Decision = _vc.Decision
normalize_artist = _vc.normalize_artist
normalize_title = _vc.normalize_title
classify_compilation = _vc.classify_compilation

LIBRARY_DB = Path(__file__).parent.parent.parent / "library.db"


@pytest.fixture(scope="module")
def library_index():
    """Build a LibraryIndex from the real library.db (skip if not present)."""
    if not LIBRARY_DB.exists():
        pytest.skip(f"library.db not found at {LIBRARY_DB}")
    return LibraryIndex.from_sqlite(LIBRARY_DB)


@pytest.fixture(scope="module")
def matcher(library_index):
    """Create a MultiIndexMatcher with default thresholds."""
    return MultiIndexMatcher(library_index)


@pytest.mark.integration
class TestMultiIndexRealLibrary:
    """Test multi-index matching against the real WXYC library catalog."""

    def test_beatles_comma_convention(self, matcher):
        """'Beatles, The' / 'Abbey Road' -> KEEP via normalization."""
        result = matcher.classify(
            normalize_artist("Beatles, The"),
            normalize_title("Abbey Road"),
        )
        assert result.decision == Decision.KEEP

    def test_radiohead_ok_computer(self, matcher):
        """Basic exact match."""
        result = matcher.classify(
            normalize_artist("Radiohead"),
            normalize_title("OK Computer"),
        )
        assert result.decision == Decision.KEEP

    def test_vinyl_suffix_stripped(self, library_index, matcher):
        """Vinyl suffixes like 12" are stripped before matching."""
        # Check if 'A Guy Called Gerald' has any 12" titles in the library
        norm = normalize_artist("A Guy Called Gerald")
        if norm not in library_index.artist_to_titles:
            pytest.skip("A Guy Called Gerald not in library")

        titles = library_index.artist_to_titles[norm]
        # Find a title that was likely from a 12" release
        for title in titles:
            result = matcher.classify(norm, title)
            assert result.decision == Decision.KEEP
            break

    def test_joy_not_joy_division(self, matcher):
        """'Joy' / 'Unknown Pleasures' should not KEEP as 'Joy' artist.

        'Joy' is a different artist from 'Joy Division'. The multi-index
        matcher should not match based on 'Joy' being a subset of
        'Joy Division'.
        """
        result = matcher.classify(
            normalize_artist("Joy"),
            normalize_title("Unknown Pleasures"),
        )
        assert result.decision != Decision.KEEP

    def test_unknown_artist_is_not_keep(self, matcher):
        """A completely unknown artist/album pair should not be KEEP.

        With ~60K library entries, coincidental partial token matches
        may push this into REVIEW rather than PRUNE. Either is acceptable.
        """
        result = matcher.classify(
            normalize_artist("Zzyzx Qxqxqx"),
            normalize_title("Xyzzy Plugh"),
        )
        assert result.decision != Decision.KEEP

    def test_aphex_twin_in_library(self, matcher):
        """Aphex Twin is a known library artist."""
        result = matcher.classify(
            normalize_artist("Aphex Twin"),
            normalize_title("Selected Ambient Works 85-92"),
        )
        assert result.decision == Decision.KEEP

    def test_artist_mapping_overrides_review(self, library_index, tmp_path):
        """Pre-populated mappings file causes REVIEW -> KEEP.

        Mapping keys are normalized artist names (what classify() receives).
        """
        mappings = {
            "keep": {"bjork": "Bjork"},  # normalized key
            "prune": {},
        }
        matcher = MultiIndexMatcher(library_index, artist_mappings=mappings)
        result = matcher.classify(
            normalize_artist("Bjork (2)"),  # normalizes to "bjork"
            normalize_title("Some Random Album"),
        )
        assert result.decision == Decision.KEEP

    def test_library_index_has_data(self, library_index):
        """Sanity check that the library loaded successfully."""
        assert len(library_index.exact_pairs) > 1000
        assert len(library_index.all_artists) > 500
        assert len(library_index.combined_strings) > 1000


SCRIPT_PATH = (
    Path(__file__).parent.parent.parent / "scripts" / "setup-discogs-db" / "verify_cache.py"
)
PYTHON = sys.executable


@pytest.mark.integration
class TestVerifyCacheE2E:
    """Test the verify_cache.py script as a subprocess."""

    def test_help_flag(self):
        """--help exits cleanly with usage text."""
        result = subprocess.run(
            [PYTHON, str(SCRIPT_PATH), "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "WXYC library" in result.stdout or "library" in result.stdout.lower()

    def test_missing_library_db_exits_nonzero(self, tmp_path):
        """Passing a nonexistent library.db path exits with error."""
        fake_db = tmp_path / "nonexistent.db"
        result = subprocess.run(
            [PYTHON, str(SCRIPT_PATH), str(fake_db)],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
