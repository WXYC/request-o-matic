"""Unit tests for library/router.py."""
import pytest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from library.models import LibraryItem
from library.router import router


@pytest.fixture
def mock_library_db():
    """Create a mock library database."""
    db = AsyncMock()
    db.search = AsyncMock(return_value=[])
    db._conn = Mock()
    return db


@pytest.fixture
def app(mock_library_db):
    """Create a test app with the library router."""
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    # Override the dependency
    from core.dependencies import get_library_db
    app.dependency_overrides[get_library_db] = lambda: mock_library_db

    return app


@pytest.fixture
def sample_library_items():
    """Sample library items for testing."""
    return [
        LibraryItem(
            id=1,
            artist="Queen",
            title="A Night at the Opera",
            call_letters="Q",
            artist_call_number=1,
            release_call_number=1,
            genre="Rock",
            format="CD",
        ),
        LibraryItem(
            id=2,
            artist="Queen",
            title="The Game",
            call_letters="Q",
            artist_call_number=1,
            release_call_number=2,
            genre="Rock",
            format="CD",
        ),
    ]


class TestSearchLibrary:
    """Tests for the search_library endpoint."""

    @pytest.mark.asyncio
    async def test_search_with_query(self, app, mock_library_db, sample_library_items):
        """Test searching with a full-text query."""
        mock_library_db.search.return_value = sample_library_items

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/library/search?q=Queen")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert len(data["results"]) == 2
        assert data["query"] == "Queen"
        mock_library_db.search.assert_called_once_with(
            query="Queen", artist=None, title=None, limit=10
        )

    @pytest.mark.asyncio
    async def test_search_with_artist_filter(self, app, mock_library_db, sample_library_items):
        """Test searching with artist filter."""
        mock_library_db.search.return_value = sample_library_items

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/library/search?artist=Queen")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        mock_library_db.search.assert_called_once_with(
            query=None, artist="Queen", title=None, limit=10
        )

    @pytest.mark.asyncio
    async def test_search_with_title_filter(self, app, mock_library_db, sample_library_items):
        """Test searching with title filter."""
        mock_library_db.search.return_value = sample_library_items[:1]

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/library/search?title=Opera")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        mock_library_db.search.assert_called_once_with(
            query=None, artist=None, title="Opera", limit=10
        )

    @pytest.mark.asyncio
    async def test_search_with_combined_filters(self, app, mock_library_db, sample_library_items):
        """Test searching with multiple filters."""
        mock_library_db.search.return_value = sample_library_items[:1]

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/v1/library/search?q=Bohemian&artist=Queen&title=Night"
            )

        assert response.status_code == 200
        mock_library_db.search.assert_called_once_with(
            query="Bohemian", artist="Queen", title="Night", limit=10
        )

    @pytest.mark.asyncio
    async def test_search_with_custom_limit(self, app, mock_library_db):
        """Test searching with custom limit."""
        mock_library_db.search.return_value = []

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/library/search?q=test&limit=50")

        assert response.status_code == 200
        mock_library_db.search.assert_called_once_with(
            query="test", artist=None, title=None, limit=50
        )

    @pytest.mark.asyncio
    async def test_search_no_params_returns_400(self, app, mock_library_db):
        """Test that searching without any parameters returns 400."""
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/library/search")

        assert response.status_code == 400
        assert "At least one of q, artist, or title must be provided" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_search_empty_results(self, app, mock_library_db):
        """Test searching with no results."""
        mock_library_db.search.return_value = []

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/library/search?q=NonexistentArtist")

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["results"] == []

    @pytest.mark.asyncio
    async def test_search_database_error_returns_500(self, app, mock_library_db):
        """Test that database errors return 500."""
        mock_library_db.search.side_effect = Exception("Database connection failed")

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/library/search?q=test")

        assert response.status_code == 500
        assert "Internal server error" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_search_limit_bounds(self, app, mock_library_db):
        """Test that limit has valid bounds (1-100)."""
        mock_library_db.search.return_value = []

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test"
        ) as client:
            # Test limit below minimum
            response = await client.get("/api/v1/library/search?q=test&limit=0")
            assert response.status_code == 422  # Validation error

            # Test limit above maximum
            response = await client.get("/api/v1/library/search?q=test&limit=101")
            assert response.status_code == 422  # Validation error

    @pytest.mark.asyncio
    async def test_search_query_format_with_artist_and_title(self, app, mock_library_db):
        """Test that query string is formatted correctly for artist+title searches."""
        mock_library_db.search.return_value = []

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/library/search?artist=Queen&title=Opera")

        assert response.status_code == 200
        data = response.json()
        # When q is not provided, query should show artist and title
        assert "artist=Queen" in data["query"]
        assert "title=Opera" in data["query"]
