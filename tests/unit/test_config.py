"""Unit tests for configuration module."""

from config.settings import Settings


def test_settings_defaults():
    """Test that settings have sensible defaults."""
    settings = Settings(groq_api_key="test_key")

    assert settings.groq_api_key == "test_key"
    assert settings.port == 8000
    assert settings.host == "0.0.0.0"
    assert settings.log_level == "INFO"
    assert settings.enable_slack_integration is True
    assert settings.app_name == "Request-O-Matic"
    assert settings.app_version == "1.0.0"


def test_settings_optional_fields():
    """Test that optional fields can be None."""
    settings = Settings(
        groq_api_key="test_key",
        slack_webhook_url=None,
    )

    assert settings.slack_webhook_url is None


def test_settings_custom_values():
    """Test that custom values override defaults."""
    settings = Settings(
        groq_api_key="test_key",
        port=9000,
        log_level="DEBUG",
        enable_slack_integration=False,
    )

    assert settings.port == 9000
    assert settings.log_level == "DEBUG"
    assert settings.enable_slack_integration is False
