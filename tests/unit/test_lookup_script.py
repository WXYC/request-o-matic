"""Unit tests for the lookup script utilities."""
import pytest
import sys
from pathlib import Path

# Add scripts directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

from lookup import filter_results_by_artist


class TestFilterResultsByArtist:
    """Tests for the filter_results_by_artist function."""

    def test_filters_out_non_matching_artists(self):
        """Test that results not matching the artist are filtered out."""
        results = [
            {"id": 1, "artist": "Biz Markie", "title": "Young Girl Bluez 12\""},
            {"id": 2, "artist": "Young Black Teenagers", "title": "Proud to be Black"},
            {"id": 3, "artist": "Young Gov", "title": "Some Album"},
        ]

        filtered = filter_results_by_artist(results, "Young Gov")

        assert len(filtered) == 1
        assert filtered[0]["artist"] == "Young Gov"

    def test_keeps_matching_artists(self):
        """Test that results matching the artist are kept."""
        results = [
            {"id": 1, "artist": "Radiohead", "title": "OK Computer"},
            {"id": 2, "artist": "Radiohead", "title": "The Bends"},
            {"id": 3, "artist": "Radiohead", "title": "Kid A"},
        ]

        filtered = filter_results_by_artist(results, "Radiohead")

        assert len(filtered) == 3

    def test_case_insensitive_matching(self):
        """Test that artist matching is case insensitive."""
        results = [
            {"id": 1, "artist": "RADIOHEAD", "title": "OK Computer"},
            {"id": 2, "artist": "radiohead", "title": "The Bends"},
            {"id": 3, "artist": "Radiohead", "title": "Kid A"},
        ]

        filtered = filter_results_by_artist(results, "radiohead")

        assert len(filtered) == 3

    def test_partial_match_in_artist_field(self):
        """Test that partial matches work (artist name contained in field)."""
        results = [
            {"id": 1, "artist": "Various Artists - Rock - D", "title": "Disco Not Disco"},
            {"id": 2, "artist": "Queen", "title": "A Night at the Opera"},
        ]

        # "Various" should match "Various Artists - Rock - D"
        filtered = filter_results_by_artist(results, "Various")

        assert len(filtered) == 1
        assert "Various" in filtered[0]["artist"]

    def test_empty_results_returns_empty(self):
        """Test that empty input returns empty output."""
        results = []

        filtered = filter_results_by_artist(results, "Any Artist")

        assert len(filtered) == 0

    def test_no_artist_returns_all(self):
        """Test that empty/None artist returns all results unfiltered."""
        results = [
            {"id": 1, "artist": "Radiohead", "title": "OK Computer"},
            {"id": 2, "artist": "Queen", "title": "The Game"},
        ]

        filtered = filter_results_by_artist(results, "")
        assert len(filtered) == 2

        filtered = filter_results_by_artist(results, None)
        assert len(filtered) == 2

    def test_handles_none_artist_in_results(self):
        """Test that results with None artist are handled gracefully."""
        results = [
            {"id": 1, "artist": None, "title": "Unknown Album"},
            {"id": 2, "artist": "Radiohead", "title": "OK Computer"},
        ]

        filtered = filter_results_by_artist(results, "Radiohead")

        assert len(filtered) == 1
        assert filtered[0]["artist"] == "Radiohead"

    def test_young_gov_scenario(self):
        """Test the specific Young Gov scenario that was failing."""
        results = [
            {"id": 1, "artist": "Biz Markie", "title": "Young Girl Bluez 12\""},
            {"id": 2, "artist": "Young Black Teenagers", "title": "Proud to be Black 12\""},
            {"id": 3, "artist": "Young Black Teenagers", "title": "Young Black Teenagers"},
            {"id": 4, "artist": "Young Black Teenagers", "title": "Loud and Hard to Hit 12\""},
            {"id": 5, "artist": "Young Black Teenagers", "title": "Dead End Kidz Doin Lifetime Biz"},
        ]

        filtered = filter_results_by_artist(results, "Young Gov")

        # None of these should match "Young Gov"
        assert len(filtered) == 0

    def test_laid_back_scenario(self):
        """Test the Laid Back scenario - should not match albums with 'laid back' in title."""
        results = [
            {"id": 1, "artist": "Various Artists - Hiphop", "title": "Night Shift - Laid Back Trip Hop"},
            {"id": 2, "artist": "Gregg Allman", "title": "Laid Back"},
            {"id": 3, "artist": "Beatnik Filmstars", "title": "Laid Back and English"},
            {"id": 4, "artist": "Laid Back", "title": "Keep Smiling"},
        ]

        filtered = filter_results_by_artist(results, "Laid Back")

        # Only the actual band "Laid Back" should match
        assert len(filtered) == 1
        assert filtered[0]["artist"] == "Laid Back"
