"""Unit tests for routers/health.py."""

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from config.settings import Settings
from routers.health import router


@pytest.fixture
def mock_settings():
    """Create mock settings."""
    return Settings(
        groq_api_key="test_groq_key",
        discogs_token="test_discogs_token",
        slack_webhook_url="https://hooks.slack.com/test",
        library_db_path=Path("test_library.db"),
        log_level="DEBUG",
        enable_slack_integration=True,
        enable_artwork_lookup=True,
        app_version="1.0.0-test",
    )


@pytest.fixture
def mock_library_db():
    """Create a mock library database."""
    db = AsyncMock()
    db._conn = Mock()  # Connected state
    return db


@pytest.fixture
def mock_discogs_service():
    """Create a mock Discogs service."""
    return Mock()


@pytest.fixture
def app(mock_settings, mock_library_db, mock_discogs_service):
    """Create a test app with the health router."""
    app = FastAPI()
    app.include_router(router)

    # Override dependencies
    from config.settings import get_settings
    from core.dependencies import get_discogs_service, get_library_db

    app.dependency_overrides[get_settings] = lambda: mock_settings
    app.dependency_overrides[get_library_db] = lambda: mock_library_db
    app.dependency_overrides[get_discogs_service] = lambda: mock_discogs_service

    return app


class TestHealthCheck:
    """Tests for the health check endpoint."""

    @pytest.mark.asyncio
    async def test_health_check_all_services_healthy(
        self, app, mock_settings, mock_library_db, mock_discogs_service
    ):
        """Test health check when all services are healthy."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")

        assert response.status_code == 200
        data = response.json()

        assert data["status"] == "healthy"
        assert data["version"] == "1.0.0-test"
        assert data["services"]["groq"] == "configured"
        assert data["services"]["database"] == "connected"
        assert data["services"]["discogs"] == "available"
        assert data["services"]["slack"] == "enabled"

    @pytest.mark.asyncio
    async def test_health_check_groq_not_configured(self, mock_library_db, mock_discogs_service):
        """Test health check when Groq API key is not configured."""
        settings = Settings(
            groq_api_key="",  # Empty = not configured
            discogs_token="test_token",
            enable_slack_integration=True,
            enable_artwork_lookup=True,
        )

        app = FastAPI()
        app.include_router(router)

        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_library_db] = lambda: mock_library_db
        app.dependency_overrides[get_discogs_service] = lambda: mock_discogs_service

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["services"]["groq"] == "not_configured"

    @pytest.mark.asyncio
    async def test_health_check_database_disconnected(self, mock_settings, mock_discogs_service):
        """Test health check when database is disconnected."""
        mock_db = AsyncMock()
        mock_db._conn = None  # Disconnected state

        app = FastAPI()
        app.include_router(router)

        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db

        app.dependency_overrides[get_settings] = lambda: mock_settings
        app.dependency_overrides[get_library_db] = lambda: mock_db
        app.dependency_overrides[get_discogs_service] = lambda: mock_discogs_service

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["services"]["database"] == "disconnected"

    @pytest.mark.asyncio
    async def test_health_check_discogs_unavailable(self, mock_settings, mock_library_db):
        """Test health check when Discogs service is unavailable."""
        settings = Settings(
            groq_api_key="test_key",
            discogs_token=None,  # No token
            enable_artwork_lookup=True,
        )

        app = FastAPI()
        app.include_router(router)

        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_library_db] = lambda: mock_library_db
        app.dependency_overrides[get_discogs_service] = lambda: None

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["services"]["discogs"] == "unavailable"

    @pytest.mark.asyncio
    async def test_health_check_discogs_disabled(self, mock_settings, mock_library_db):
        """Test health check when artwork lookup is disabled."""
        settings = Settings(
            groq_api_key="test_key",
            discogs_token="test_token",
            enable_artwork_lookup=False,  # Disabled
        )

        app = FastAPI()
        app.include_router(router)

        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_library_db] = lambda: mock_library_db
        app.dependency_overrides[get_discogs_service] = lambda: Mock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["services"]["discogs"] == "disabled"

    @pytest.mark.asyncio
    async def test_health_check_slack_disabled(
        self, mock_settings, mock_library_db, mock_discogs_service
    ):
        """Test health check when Slack integration is disabled."""
        settings = Settings(
            groq_api_key="test_key",
            discogs_token="test_token",
            enable_slack_integration=False,  # Disabled
            enable_artwork_lookup=True,
        )

        app = FastAPI()
        app.include_router(router)

        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_library_db] = lambda: mock_library_db
        app.dependency_overrides[get_discogs_service] = lambda: mock_discogs_service

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["services"]["slack"] == "disabled"

    @pytest.mark.asyncio
    async def test_health_check_features_enabled(self, app, mock_settings):
        """Test that features are correctly reported."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")

        assert response.status_code == 200
        data = response.json()

        assert data["features"]["artwork_lookup"] is True
        assert data["features"]["slack_integration"] is True

    @pytest.mark.asyncio
    async def test_health_check_features_disabled(self, mock_library_db, mock_discogs_service):
        """Test features when disabled."""
        settings = Settings(
            groq_api_key="test_key",
            enable_artwork_lookup=False,
            enable_slack_integration=False,
        )

        app = FastAPI()
        app.include_router(router)

        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db

        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_library_db] = lambda: mock_library_db
        app.dependency_overrides[get_discogs_service] = lambda: mock_discogs_service

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/health")

        assert response.status_code == 200
        data = response.json()

        assert data["features"]["artwork_lookup"] is False
        assert data["features"]["slack_integration"] is False
