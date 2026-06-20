"""Unit tests for main.py."""

from unittest.mock import patch


def _openapi_paths(app) -> set[str]:
    """Registered URL paths, read from the OpenAPI schema.

    FastAPI >=0.137 includes sub-routers lazily as ``fastapi.routing._IncludedRouter``
    wrappers that don't expose ``.path`` on ``app.routes`` (older FastAPI eagerly
    flattened sub-router routes into ``app.routes`` with a ``.path``). Introspecting
    ``app.routes``/``.path`` therefore silently misses every included route. The
    OpenAPI schema reflects every registered path regardless of inclusion strategy,
    so it is the version-stable surface to assert against.
    """
    return set(app.openapi().get("paths", {}).keys())


def _operation_tags(app, path: str) -> set[str]:
    """Union of OpenAPI tags across every HTTP method of ``path``."""
    operations = app.openapi().get("paths", {}).get(path, {})
    tags: set[str] = set()
    for operation in operations.values():
        tags.update(operation.get("tags", []) or [])
    return tags


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

            paths = _openapi_paths(app)

            # Check for health endpoints
            assert "/health" in paths
            assert "/health/ready" in paths

            # Check for versioned API routes
            assert "/api/v1/request" in paths
            assert "/api/v1/parse" in paths

    def test_app_has_legacy_routes(self):
        """Test that app has legacy (non-versioned) routes for backwards compatibility."""
        with patch.dict("os.environ", {"GROQ_API_KEY": "test_key"}):
            from config.settings import get_settings

            get_settings.cache_clear()

            from main import app

            paths = _openapi_paths(app)

            # Check for legacy routes
            assert any("/request" in path and "/api/v1" not in path for path in paths)
            assert any("/parse" in path and "/api/v1" not in path for path in paths)

    def test_app_has_admin_bans_routes(self):
        """Pin that ``/admin/bans`` (#151) is mounted at the root, not under /api/v1.

        Operators curl ``/admin/bans`` directly — burying it under ``/api/v1``
        would diverge from LML's admin pattern and from the operator runbook
        in ``docs/admin-bans.md``.
        """
        with patch.dict("os.environ", {"GROQ_API_KEY": "test_key"}):
            from config.settings import get_settings

            get_settings.cache_clear()

            from main import app

            paths = _openapi_paths(app)

            assert "/admin/bans" in paths
            assert "/admin/bans/{fingerprint}" in paths
            # Must NOT have been accidentally namespaced under /api/v1
            assert not any("/api/v1/admin" in path for path in paths)

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
        """Test that the health route is tagged "health"."""
        with patch.dict("os.environ", {"GROQ_API_KEY": "test_key"}):
            from config.settings import get_settings

            get_settings.cache_clear()

            from main import app

            assert "/health" in _openapi_paths(app), "/health route is not registered"
            assert "health" in _operation_tags(app, "/health")

    def test_versioned_routes_have_tags(self):
        """Test that versioned routes exist and carry tags."""
        with patch.dict("os.environ", {"GROQ_API_KEY": "test_key"}):
            from config.settings import get_settings

            get_settings.cache_clear()

            from main import app

            paths = _openapi_paths(app)

            api_paths = [path for path in paths if "/api/v1" in path]
            assert len(api_paths) > 0  # Should have versioned routes
            # Every versioned route should carry at least one tag.
            for path in api_paths:
                assert _operation_tags(app, path), f"versioned route {path} has no tags"


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
