"""Tests for Sentry integration."""

from unittest.mock import patch


class TestSentryInitialization:
    """Tests for Sentry SDK initialization."""

    def test_sentry_initializes_with_dsn(self):
        """Test that Sentry is initialized when DSN is configured."""
        with patch("sentry_sdk.init") as mock_init:
            from core.sentry import init_sentry

            init_sentry(dsn="https://test@sentry.io/123")

            mock_init.assert_called_once()
            call_kwargs = mock_init.call_args.kwargs
            assert call_kwargs["dsn"] == "https://test@sentry.io/123"

    def test_sentry_skips_init_without_dsn(self):
        """Test that Sentry initialization is skipped when DSN is None."""
        with patch("sentry_sdk.init") as mock_init:
            from core.sentry import init_sentry

            init_sentry(dsn=None)

            mock_init.assert_not_called()

    def test_sentry_includes_fastapi_integration(self):
        """Test that FastAPI integration is included."""
        with patch("sentry_sdk.init") as mock_init:
            from core.sentry import init_sentry

            init_sentry(dsn="https://test@sentry.io/123")

            call_kwargs = mock_init.call_args.kwargs
            integrations = call_kwargs.get("integrations", [])
            integration_types = [type(i).__name__ for i in integrations]
            assert "FastApiIntegration" in integration_types

    def test_sentry_includes_httpx_integration(self):
        """HttpxIntegration must be included so outbound calls to LML carry
        sentry-trace headers — otherwise request-o-matic and LML show up as
        disconnected traces in the same project."""
        with patch("sentry_sdk.init") as mock_init:
            from core.sentry import init_sentry

            init_sentry(dsn="https://test@sentry.io/123")

            call_kwargs = mock_init.call_args.kwargs
            integrations = call_kwargs.get("integrations", [])
            integration_types = [type(i).__name__ for i in integrations]
            assert "HttpxIntegration" in integration_types

    def test_sentry_sets_environment(self):
        """Test that environment is set from parameter."""
        with patch("sentry_sdk.init") as mock_init:
            from core.sentry import init_sentry

            init_sentry(dsn="https://test@sentry.io/123", environment="staging")

            call_kwargs = mock_init.call_args.kwargs
            assert call_kwargs["environment"] == "staging"


class TestSentryBreadcrumbs:
    """Tests for Sentry breadcrumb recording."""

    def test_add_breadcrumb_records_operation(self):
        """Test that add_breadcrumb records Discogs operations."""
        with patch("sentry_sdk.add_breadcrumb") as mock_add:
            from core.sentry import add_discogs_breadcrumb

            add_discogs_breadcrumb(
                operation="search_releases_by_track",
                data={"track": "Blue Monday", "artist": "New Order"},
            )

            mock_add.assert_called_once()
            call_kwargs = mock_add.call_args.kwargs
            assert call_kwargs["category"] == "discogs"
            assert call_kwargs["message"] == "search_releases_by_track"
            assert call_kwargs["data"]["track"] == "Blue Monday"
            assert call_kwargs["data"]["artist"] == "New Order"

    def test_add_breadcrumb_handles_none_data(self):
        """Test that breadcrumb works with no extra data."""
        with patch("sentry_sdk.add_breadcrumb") as mock_add:
            from core.sentry import add_discogs_breadcrumb

            add_discogs_breadcrumb(operation="get_release")

            mock_add.assert_called_once()
            call_kwargs = mock_add.call_args.kwargs
            assert call_kwargs["message"] == "get_release"
            assert call_kwargs["data"] == {}


class TestSentryExceptionCapture:
    """Tests for Sentry exception capture."""

    def test_capture_exception_sends_to_sentry(self):
        """Test that exceptions are captured and sent to Sentry."""
        with patch("sentry_sdk.capture_exception") as mock_capture:
            from core.sentry import capture_exception

            error = ValueError("Test error")
            capture_exception(error)

            mock_capture.assert_called_once_with(error)

    def test_capture_exception_with_extra_context(self):
        """Test that extra context is included in exception capture."""
        with (
            patch("sentry_sdk.set_context") as mock_context,
            patch("sentry_sdk.capture_exception") as mock_capture,
        ):
            from core.sentry import capture_exception

            error = ValueError("Test error")
            capture_exception(error, context={"release_id": 12345, "track": "Blue Monday"})

            mock_context.assert_called_once_with(
                "discogs", {"release_id": 12345, "track": "Blue Monday"}
            )
            mock_capture.assert_called_once_with(error)


class TestSentrySettingsIntegration:
    """Tests for Sentry integration with application settings."""

    def test_settings_includes_sentry_dsn(self, monkeypatch):
        """Test that Settings model includes sentry_dsn field."""
        monkeypatch.setenv("GROQ_API_KEY", "test_key")

        from config.settings import Settings

        # Use _env_file=None to skip .env file and only use env vars
        settings = Settings(sentry_dsn="https://test@sentry.io/123", _env_file=None)
        assert settings.sentry_dsn == "https://test@sentry.io/123"

    def test_settings_sentry_dsn_defaults_to_none(self, monkeypatch):
        """Test that sentry_dsn defaults to None when not set."""
        monkeypatch.setenv("GROQ_API_KEY", "test_key")
        monkeypatch.delenv("SENTRY_DSN", raising=False)

        from config.settings import Settings

        # Use _env_file=None to skip .env file and only use env vars
        settings = Settings(_env_file=None)
        assert settings.sentry_dsn is None

    def test_deployment_environment_reads_railway_env_name(self, monkeypatch):
        """RAILWAY_ENVIRONMENT_NAME (set by Railway) drives the Sentry environment
        tag. Without this, staging deploys are misreported as 'production' in
        Sentry — which is why the original Sentry investigation couldn't find
        any staging traces."""
        monkeypatch.setenv("GROQ_API_KEY", "test_key")
        monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "staging")
        monkeypatch.delenv("DEPLOYMENT_ENVIRONMENT", raising=False)

        from config.settings import Settings

        settings = Settings(_env_file=None)
        assert settings.deployment_environment == "staging"

    def test_deployment_environment_defaults_to_local(self, monkeypatch):
        """Without Railway env vars present, default to 'local' so devs running
        the server on their laptop don't pollute the production Sentry stream."""
        monkeypatch.setenv("GROQ_API_KEY", "test_key")
        monkeypatch.delenv("RAILWAY_ENVIRONMENT_NAME", raising=False)
        monkeypatch.delenv("DEPLOYMENT_ENVIRONMENT", raising=False)

        from config.settings import Settings

        settings = Settings(_env_file=None)
        assert settings.deployment_environment == "local"

    def test_deployment_environment_explicit_override(self, monkeypatch):
        """DEPLOYMENT_ENVIRONMENT wins over RAILWAY_ENVIRONMENT_NAME for cases
        where you want to manually pin the value (e.g. running prod code under
        a debug runner)."""
        monkeypatch.setenv("GROQ_API_KEY", "test_key")
        monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
        monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "shadow")

        from config.settings import Settings

        settings = Settings(_env_file=None)
        assert settings.deployment_environment == "shadow"
