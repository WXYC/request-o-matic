"""Tests for Discogs Pydantic models."""

import pytest

from discogs.models import (
    DISCOGS_RELEASE_URL_BASE,
    DiscogsSearchResult,
    ReleaseInfo,
    ReleaseMetadataResponse,
)


class TestComputedReleaseUrl:
    """Tests for computed release_url on models."""

    def test_release_info_computes_url_from_id(self):
        """Test ReleaseInfo.release_url is computed from release_id."""
        info = ReleaseInfo(album="Confield", artist="Autechre", release_id=28138)
        assert info.release_url == f"{DISCOGS_RELEASE_URL_BASE}/28138"

    def test_release_metadata_computes_url_from_id(self):
        """Test ReleaseMetadataResponse.release_url is computed from release_id."""
        meta = ReleaseMetadataResponse(release_id=28138, title="Confield", artist="Autechre")
        assert meta.release_url == f"{DISCOGS_RELEASE_URL_BASE}/28138"

    def test_search_result_computes_url_from_id(self):
        """Test DiscogsSearchResult.release_url is computed from release_id."""
        result = DiscogsSearchResult(release_id=28138)
        assert result.release_url == f"{DISCOGS_RELEASE_URL_BASE}/28138"

    @pytest.mark.parametrize(
        "release_id,expected_url",
        [
            (1, f"{DISCOGS_RELEASE_URL_BASE}/1"),
            (99999, f"{DISCOGS_RELEASE_URL_BASE}/99999"),
            (12345678, f"{DISCOGS_RELEASE_URL_BASE}/12345678"),
        ],
    )
    def test_url_varies_with_release_id(self, release_id, expected_url):
        """Test release_url changes with different release_ids."""
        info = ReleaseInfo(album="Test", artist="Test", release_id=release_id)
        assert info.release_url == expected_url

    def test_release_info_serializes_url(self):
        """Test release_url appears in model serialization."""
        info = ReleaseInfo(album="Confield", artist="Autechre", release_id=28138)
        data = info.model_dump()
        assert "release_url" in data
        assert data["release_url"] == f"{DISCOGS_RELEASE_URL_BASE}/28138"

    def test_release_url_base_constant(self):
        """Test the URL base constant is correct."""
        assert DISCOGS_RELEASE_URL_BASE == "https://www.discogs.com/release"
