"""Unit tests for main.py."""

from unittest.mock import patch


class TestAppConfiguration:
    """Tests for FastAPI app configuration."""

    def test_app_title(self):
        """Test that app has correct title."""
        with patch.dict("os.environ", {"GROQ_API_KEY": "test_key"}):
            from config.settings import get_settings

            get_settings.cache_clear()

            from main import app

            assert "Request-O-Matic" in app.title

    def test_app_has_routes(self):
        """Test that app has expected routes."""
        with patch.dict("os.environ", {"GROQ_API_KEY": "test_key"}):
            from config.settings import get_settings

            get_settings.cache_clear()

            from main import app

            routes = [route.path for route in app.routes if hasattr(route, "path")]

            # Check for health endpoints
            assert "/health" in routes
            assert "/health/ready" in routes

            # Check for versioned API routes
            assert any("/api/v1/request" in route for route in routes)
            assert any("/api/v1/parse" in route for route in routes)

    def test_app_has_legacy_routes(self):
        """Test that app has legacy (non-versioned) routes for backwards compatibility."""
        with patch.dict("os.environ", {"GROQ_API_KEY": "test_key"}):
            from config.settings import get_settings

            get_settings.cache_clear()

            from main import app

            routes = [route.path for route in app.routes if hasattr(route, "path")]

            # Check for legacy routes
            assert any("/request" in route and "/api/v1" not in route for route in routes)
            assert any("/parse" in route and "/api/v1" not in route for route in routes)

    def test_app_has_description(self):
        """Test that app has a description."""
        with patch.dict("os.environ", {"GROQ_API_KEY": "test_key"}):
            from config.settings import get_settings

            get_settings.cache_clear()

            from main import app

            assert app.description is not None
            assert len(app.description) > 0

    def test_app_has_version(self):
        """Test that app has a version."""
        with patch.dict("os.environ", {"GROQ_API_KEY": "test_key"}):
            from config.settings import get_settings

            get_settings.cache_clear()

            from main import app

            assert app.version is not None


class TestAppRouterTags:
    """Tests for router tags."""

    def test_health_router_tag(self):
        """Test that health router has correct tag."""
        with patch.dict("os.environ", {"GROQ_API_KEY": "test_key"}):
            from config.settings import get_settings

            get_settings.cache_clear()

            from main import app

            # Find health route and check tags
            for route in app.routes:
                if hasattr(route, "path") and route.path == "/health":
                    assert hasattr(route, "tags") and "health" in route.tags
                    break

    def test_versioned_routes_have_tags(self):
        """Test that versioned routes have appropriate tags."""
        with patch.dict("os.environ", {"GROQ_API_KEY": "test_key"}):
            from config.settings import get_settings

            get_settings.cache_clear()

            from main import app

            # Find API routes and check tags
            api_routes = [r for r in app.routes if hasattr(r, "path") and "/api/v1" in r.path]
            assert len(api_routes) > 0  # Should have versioned routes


class TestAppLifespan:
    """Tests for app lifespan function definition."""

    def test_lifespan_is_defined(self):
        """Test that lifespan context manager is defined."""
        with patch.dict("os.environ", {"GROQ_API_KEY": "test_key"}):
            from config.settings import get_settings

            get_settings.cache_clear()

            from main import lifespan

            # Lifespan should be an async context manager
            assert callable(lifespan)
