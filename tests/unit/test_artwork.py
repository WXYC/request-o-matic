import pytest
from pytest_httpx import HTTPXMock

from artwork.models import ArtworkRequest
from artwork.providers.discogs import DiscogsProvider
from artwork.finder import ArtworkFinder


# --- Fixtures ---


@pytest.fixture
def discogs_provider():
    return DiscogsProvider(token="test-token")


@pytest.fixture
def sample_discogs_response():
    """Sample Discogs API search response."""
    return {
        "pagination": {"items": 2},
        "results": [
            {
                "id": 123,
                "title": "The Stone Roses - The Stone Roses",
                "thumb": "https://i.discogs.com/stone-roses.jpg",
                "type": "release",
            },
            {
                "id": 456,
                "title": "The Stone Roses - Second Coming",
                "thumb": "https://i.discogs.com/second-coming.jpg",
                "type": "release",
            },
        ],
    }


@pytest.fixture
def empty_discogs_response():
    return {"pagination": {"items": 0}, "results": []}


@pytest.fixture
def discogs_response_no_artwork():
    """Response with spacer.gif (no real artwork)."""
    return {
        "pagination": {"items": 1},
        "results": [
            {
                "id": 789,
                "title": "Unknown Artist - Unknown Album",
                "thumb": "https://st.discogs.com/spacer.gif",
                "type": "release",
            }
        ],
    }


# --- DiscogsProvider Tests ---


class TestDiscogsProvider:
    @pytest.mark.asyncio
    async def test_search_with_artist_and_album(
        self,
        discogs_provider: DiscogsProvider,
        httpx_mock: HTTPXMock,
        sample_discogs_response,
    ):
        """Test search with both artist and album returns results."""
        httpx_mock.add_response(json=sample_discogs_response)

        request = ArtworkRequest(artist="The Stone Roses", album="The Stone Roses")
        results = await discogs_provider.search(request)

        assert len(results) == 2
        assert results[0].artwork_url == "https://i.discogs.com/stone-roses.jpg"
        assert results[0].release_url == "https://www.discogs.com/release/123"
        assert results[0].artist == "The Stone Roses"
        assert results[0].album == "The Stone Roses"
        assert results[0].source == "discogs"

        await discogs_provider.close()

    @pytest.mark.asyncio
    async def test_search_artist_only(
        self,
        discogs_provider: DiscogsProvider,
        httpx_mock: HTTPXMock,
        sample_discogs_response,
    ):
        """Test search with only artist."""
        httpx_mock.add_response(json=sample_discogs_response)

        request = ArtworkRequest(artist="The Stone Roses")
        results = await discogs_provider.search(request)

        assert len(results) == 2
        await discogs_provider.close()

    @pytest.mark.asyncio
    async def test_search_song_and_artist(
        self,
        discogs_provider: DiscogsProvider,
        httpx_mock: HTTPXMock,
        sample_discogs_response,
    ):
        """Test search with song and artist (no album)."""
        httpx_mock.add_response(json=sample_discogs_response)

        request = ArtworkRequest(song="She Bangs the Drums", artist="The Stone Roses")
        results = await discogs_provider.search(request)

        assert len(results) == 2
        await discogs_provider.close()

    @pytest.mark.asyncio
    async def test_search_no_results(
        self,
        discogs_provider: DiscogsProvider,
        httpx_mock: HTTPXMock,
        empty_discogs_response,
    ):
        """Test search with no results."""
        # Mock both the initial search and the fallback fuzzy search
        httpx_mock.add_response(json=empty_discogs_response)
        httpx_mock.add_response(json=empty_discogs_response)  # Fallback response

        request = ArtworkRequest(artist="Nonexistent Band")
        results = await discogs_provider.search(request)

        assert len(results) == 0
        await discogs_provider.close()

    @pytest.mark.asyncio
    async def test_search_filters_spacer_gif(
        self,
        discogs_provider: DiscogsProvider,
        httpx_mock: HTTPXMock,
        discogs_response_no_artwork,
    ):
        """Test that spacer.gif results are filtered out."""
        httpx_mock.add_response(json=discogs_response_no_artwork)

        request = ArtworkRequest(artist="Unknown Artist")
        results = await discogs_provider.search(request)

        assert len(results) == 0
        await discogs_provider.close()

    @pytest.mark.asyncio
    async def test_search_empty_request(self, discogs_provider: DiscogsProvider):
        """Test search with no fields returns empty results."""
        request = ArtworkRequest()
        results = await discogs_provider.search(request)

        assert len(results) == 0
        await discogs_provider.close()

    @pytest.mark.asyncio
    async def test_search_handles_rate_limit(
        self, discogs_provider: DiscogsProvider, httpx_mock: HTTPXMock
    ):
        """Test that 429 rate limit is handled gracefully."""
        httpx_mock.add_response(status_code=429)

        request = ArtworkRequest(artist="The Stone Roses")
        results = await discogs_provider.search(request)

        assert len(results) == 0
        await discogs_provider.close()

    @pytest.mark.asyncio
    async def test_confidence_scoring_exact_match(
        self, discogs_provider: DiscogsProvider, httpx_mock: HTTPXMock
    ):
        """Test that exact matches get higher confidence."""
        httpx_mock.add_response(
            json={
                "pagination": {"items": 1},
                "results": [
                    {
                        "id": 999,
                        "type": "release",
                        "title": "The Stone Roses - The Stone Roses",
                        "thumb": "https://example.com/cover.jpg",
                    }
                ],
            }
        )

        request = ArtworkRequest(artist="The Stone Roses", album="The Stone Roses")
        results = await discogs_provider.search(request)

        assert len(results) == 1
        assert results[0].confidence >= 0.8
        assert results[0].release_url == "https://www.discogs.com/release/999"
        await discogs_provider.close()


# --- ArtworkFinder Tests ---


class TestArtworkFinder:
    @pytest.mark.asyncio
    async def test_find_returns_best_result(self, httpx_mock: HTTPXMock):
        """Test finder returns highest confidence result."""
        httpx_mock.add_response(
            json={
                "pagination": {"items": 1},
                "results": [
                    {
                        "id": 12345,
                        "type": "release",
                        "title": "The Stone Roses - The Stone Roses",
                        "thumb": "https://example.com/cover.jpg",
                    }
                ],
            }
        )

        provider = DiscogsProvider(token="test")
        finder = ArtworkFinder(providers=[provider])

        request = ArtworkRequest(artist="The Stone Roses", album="The Stone Roses")
        response = await finder.find(request)

        assert response.artwork_url == "https://example.com/cover.jpg"
        assert response.release_url == "https://www.discogs.com/release/12345"
        assert response.source == "discogs"
        assert response.confidence > 0
        await provider.close()

    @pytest.mark.asyncio
    async def test_find_empty_request(self):
        """Test finder handles empty request."""
        finder = ArtworkFinder(providers=[])

        request = ArtworkRequest()
        response = await finder.find(request)

        assert response.artwork_url is None
        assert response.confidence == 0.0

    @pytest.mark.asyncio
    async def test_find_no_providers(self):
        """Test finder with no providers returns empty response."""
        finder = ArtworkFinder(providers=[])

        request = ArtworkRequest(artist="The Stone Roses")
        response = await finder.find(request)

        assert response.artwork_url is None


# --- Search Params Tests ---


class TestSearchParams:
    def test_params_album_and_artist(self, discogs_provider: DiscogsProvider):
        """Album + artist uses specific fields."""
        request = ArtworkRequest(album="The Stone Roses", artist="The Stone Roses")
        params = discogs_provider._build_search_params(request)
        assert params["artist"] == "The Stone Roses"
        assert params["release_title"] == "The Stone Roses"
        assert params["type"] == "release"

    def test_params_song_and_artist(self, discogs_provider: DiscogsProvider):
        """Song + artist uses song as release_title fallback."""
        request = ArtworkRequest(song="She Bangs the Drums", artist="The Stone Roses")
        params = discogs_provider._build_search_params(request)
        assert params["artist"] == "The Stone Roses"
        assert params["release_title"] == "She Bangs the Drums"

    def test_params_artist_only(self, discogs_provider: DiscogsProvider):
        """Artist-only search."""
        request = ArtworkRequest(artist="The Stone Roses")
        params = discogs_provider._build_search_params(request)
        assert params["artist"] == "The Stone Roses"
        assert "release_title" not in params

    def test_params_album_takes_precedence_over_song(
        self, discogs_provider: DiscogsProvider
    ):
        """When album is present, it's used instead of song for release_title."""
        request = ArtworkRequest(
            song="She Bangs the Drums",
            album="The Stone Roses",
            artist="The Stone Roses",
        )
        params = discogs_provider._build_search_params(request)
        assert params["release_title"] == "The Stone Roses"

    def test_params_empty_request(self, discogs_provider: DiscogsProvider):
        """Empty request returns empty params."""
        request = ArtworkRequest()
        params = discogs_provider._build_search_params(request)
        assert params == {}


# --- Track Search Tests ---


class TestTrackSearch:
    @pytest.mark.asyncio
    async def test_search_releases_by_track(
        self, discogs_provider: DiscogsProvider, httpx_mock: HTTPXMock
    ):
        """Test searching for all releases containing a specific track."""
        # Mock search response with multiple releases containing the same track
        # Request 1: Track search
        httpx_mock.add_response(
            json={
                "pagination": {"items": 3},
                "results": [
                    {
                        "id": 1,
                        "title": "Manu Dibango - Electric Africa",
                        "thumb": "https://example.com/electric-africa.jpg",
                        "type": "release",
                    },
                    {
                        "id": 2,
                        "title": "Various - Celluloid Records- change the beat 1979-87",
                        "thumb": "https://example.com/celluloid.jpg",
                        "type": "release",
                    },
                    {
                        "id": 3,
                        "title": "Various Artists - Best of Afro Funk",
                        "thumb": "https://example.com/afro-funk.jpg",
                        "type": "release",
                    },
                ],
            },
        )

        # Request 2: Tracklist validation for release 2 (Various)
        httpx_mock.add_response(
            json={
                "tracklist": [
                    {
                        "title": "Abele Dance (85 Remix)",
                        "artists": [{"name": "Manu Dibango"}],
                    },
                    {
                        "title": "Change The Beat",
                        "artists": [{"name": "Fab 5 Freddy"}],
                    },
                ],
            },
        )

        # Request 3: Tracklist validation for release 3 (Various Artists)
        httpx_mock.add_response(
            json={
                "tracklist": [
                    {
                        "title": "Abele Dance (85 Remix)",
                        "artists": [{"name": "Manu Dibango"}],
                    },
                    {
                        "title": "Soul Makossa",
                        "artists": [{"name": "Manu Dibango"}],
                    },
                ],
            },
        )

        releases = await discogs_provider.search_releases_by_track(
            "Abele Dance (85 Remix)", "Manu Dibango"
        )

        assert len(releases) == 3
        assert releases[0] == ("Manu Dibango", "Electric Africa")
        assert releases[1] == ("Various", "Celluloid Records- change the beat 1979-87")
        assert releases[2] == ("Various Artists", "Best of Afro Funk")

        await discogs_provider.close()

    @pytest.mark.asyncio
    async def test_search_releases_filters_invalid_compilations(
        self, discogs_provider: DiscogsProvider, httpx_mock: HTTPXMock
    ):
        """Test that Various Artists releases without the actual track are filtered out."""
        # Request 1: Track search
        httpx_mock.add_response(
            json={
                "pagination": {"items": 2},
                "results": [
                    {
                        "id": 1,
                        "title": "Sugar Plant - After Come Down",
                        "thumb": "https://example.com/sugar-plant.jpg",
                        "type": "release",
                    },
                    {
                        "id": 2,
                        "title": "Various - 22 Explosive Hits, Vol 2",
                        "thumb": "https://example.com/hits.jpg",
                        "type": "release",
                    },
                ],
            },
        )

        # Request 2: Tracklist for compilation - does NOT contain the actual track/artist
        httpx_mock.add_response(
            json={
                "tracklist": [
                    {
                        "title": "A Simple Man",  # Similar but not "Simple"
                        "artists": [{"name": "Sugar Bears (2)"}],  # Similar but not "Sugar Plant"
                    },
                    {
                        "title": "Explosive Hit",
                        "artists": [{"name": "Some Other Artist"}],
                    },
                ],
            },
        )

        # Request 3: Keyword fallback search (since < 3 results after validation)
        httpx_mock.add_response(
            json={
                "pagination": {"items": 0},
                "results": [],
            },
        )

        releases = await discogs_provider.search_releases_by_track("Simple", "Sugar Plant")

        # Only the Sugar Plant album should be returned, compilation filtered out
        assert len(releases) == 1
        assert releases[0] == ("Sugar Plant", "After Come Down")

        await discogs_provider.close()

    @pytest.mark.asyncio
    async def test_search_releases_by_track_no_artist(
        self, discogs_provider: DiscogsProvider, httpx_mock: HTTPXMock
    ):
        """Test searching for track without artist filter."""
        # First request: track search returns 2 results (triggers fallback)
        httpx_mock.add_response(
            json={
                "pagination": {"items": 2},
                "results": [
                    {
                        "id": 100,
                        "title": "Artist A - Album X",
                        "thumb": "https://example.com/x.jpg",
                        "type": "release",
                    },
                    {
                        "id": 101,
                        "title": "Artist B - Album Y",
                        "thumb": "https://example.com/y.jpg",
                        "type": "release",
                    },
                ],
            }
        )
        # Second request: keyword fallback (returns same results, deduplicated)
        httpx_mock.add_response(
            json={
                "pagination": {"items": 2},
                "results": [
                    {
                        "id": 100,
                        "title": "Artist A - Album X",
                        "thumb": "https://example.com/x.jpg",
                        "type": "release",
                    },
                    {
                        "id": 102,
                        "title": "Artist C - Album Z",
                        "thumb": "https://example.com/z.jpg",
                        "type": "release",
                    },
                ],
            }
        )

        releases = await discogs_provider.search_releases_by_track("Common Song Title")

        # Should have 3: Album X, Album Y from track search, Album Z from keyword search
        assert len(releases) == 3
        assert releases[0] == ("Artist A", "Album X")
        assert releases[1] == ("Artist B", "Album Y")
        assert releases[2] == ("Artist C", "Album Z")

        await discogs_provider.close()

    @pytest.mark.asyncio
    async def test_search_releases_by_track_no_results(
        self, discogs_provider: DiscogsProvider, httpx_mock: HTTPXMock
    ):
        """Test track search with no results."""
        # First request: track search returns nothing
        httpx_mock.add_response(json={"pagination": {"items": 0}, "results": []})
        # Second request: keyword fallback also returns nothing
        httpx_mock.add_response(json={"pagination": {"items": 0}, "results": []})

        releases = await discogs_provider.search_releases_by_track(
            "Nonexistent Song", "Unknown Artist"
        )

        assert len(releases) == 0
        await discogs_provider.close()

    @pytest.mark.asyncio
    async def test_search_releases_by_track_rate_limit(
        self, discogs_provider: DiscogsProvider, httpx_mock: HTTPXMock
    ):
        """Test that rate limits are handled gracefully."""
        # First request: rate limited
        httpx_mock.add_response(status_code=429)
        # Second request: also rate limited
        httpx_mock.add_response(status_code=429)

        releases = await discogs_provider.search_releases_by_track(
            "Some Song", "Some Artist"
        )

        assert len(releases) == 0
        await discogs_provider.close()
