"""Application configuration using Pydantic Settings."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # API Keys - Required
    groq_api_key: str = Field(..., description="Groq API key for AI parsing")

    # API Keys - Optional
    discogs_token: str | None = Field(None, description="Discogs API token for artwork lookup")
    slack_webhook_url: str | None = Field(None, description="Slack webhook URL for posting results")
    slack_webhook_key_url: str = Field(
        default="https://wxyc-requests-endpoint-production.up.railway.app",
        description="URL to fetch Slack webhook key from (used when SLACK_WEBHOOK_URL is not set)",
    )

    # Database Configuration
    # Note: We use a validator to ensure empty strings default to library.db
    library_db_path: Path = Field(
        default=Path("library.db"), description="Path to SQLite library database"
    )

    @property
    def resolved_library_db_path(self) -> Path:
        """Get the library database path, handling empty env var case."""
        # Handle case where env var is set but empty
        if not str(self.library_db_path) or str(self.library_db_path) == ".":
            return Path("library.db")
        return self.library_db_path

    # Application Configuration
    host: str = Field(default="0.0.0.0", description="Host to bind the server to")
    port: int = Field(default=8000, description="Port to run the server on")
    log_level: str = Field(default="INFO", description="Logging level")

    # Feature Flags
    enable_slack_integration: bool = Field(default=True, description="Enable Slack notifications")
    enable_artwork_lookup: bool = Field(
        default=True, description="Enable artwork lookup from external APIs"
    )
    enable_telemetry: bool = Field(default=True, description="Enable PostHog telemetry")

    # PostHog Configuration
    posthog_api_key: str | None = Field(None, description="PostHog API key for telemetry")
    posthog_host: str = Field(default="https://us.i.posthog.com", description="PostHog host URL")

    # Discogs Cache Configuration
    discogs_track_cache_ttl: int = Field(
        default=3600, description="TTL in seconds for Discogs track cache (default: 1 hour)"
    )
    discogs_release_cache_ttl: int = Field(
        default=14400, description="TTL in seconds for Discogs release cache (default: 4 hours)"
    )
    discogs_search_cache_ttl: int = Field(
        default=3600, description="TTL in seconds for Discogs search cache (default: 1 hour)"
    )
    discogs_cache_maxsize: int = Field(
        default=1000, description="Maximum entries in Discogs caches"
    )

    # Application Metadata
    app_name: str = Field(default="Request-O-Matic", description="Application name")
    app_version: str = Field(default="1.0.0", description="Application version")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance.

    Returns:
        Settings: Application settings instance
    """
    return Settings()
