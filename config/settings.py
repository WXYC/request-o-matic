"""Application configuration using Pydantic Settings."""

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # API Keys - Required
    groq_api_key: str = Field(..., description="Groq API key for AI parsing")

    # API Keys - Optional
    slack_webhook_url: str | None = Field(None, description="Slack webhook URL for posting results")
    slack_webhook_key_url: str = Field(
        default="https://wxyc-requests-endpoint-production.up.railway.app",
        description="URL to fetch Slack webhook key from (used when SLACK_WEBHOOK_URL is not set)",
    )

    # Application Configuration
    host: str = Field(default="0.0.0.0", description="Host to bind the server to")
    port: int = Field(default=8000, description="Port to run the server on")
    log_level: str = Field(default="INFO", description="Logging level")

    # Feature Flags
    enable_slack_integration: bool = Field(default=True, description="Enable Slack notifications")
    enable_telemetry: bool = Field(default=True, description="Enable PostHog telemetry")

    # PostHog Configuration
    posthog_api_key: str | None = Field(None, description="PostHog API key for telemetry")
    posthog_host: str = Field(default="https://us.i.posthog.com", description="PostHog host URL")

    # Sentry Configuration
    sentry_dsn: str | None = Field(None, description="Sentry DSN for error tracking")
    deployment_environment: str = Field(
        default="local",
        validation_alias=AliasChoices("DEPLOYMENT_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME"),
        description=(
            "Deployment environment label sent to Sentry. Defaults to RAILWAY_ENVIRONMENT_NAME "
            "(set automatically by Railway), then falls back to 'local'. Set DEPLOYMENT_ENVIRONMENT "
            "to override."
        ),
    )

    # Lookup Service Delegation
    lookup_service_url: str | None = Field(
        None,
        description="Base URL of library-metadata-lookup service. When set, delegates search to this service.",
    )
    lml_api_key: str | None = Field(
        None,
        description="Bearer token sent to library-metadata-lookup. Required when LML has LML_REQUIRE_AUTH=true.",
    )

    # Admin API (request-line ban management — request-o-matic#151)
    # Co-located with the request-line surface so operators can curl/script
    # bans against the same service they already know. Auth is a single shared
    # bearer token; rom is a thin proxy and forwards writes to Backend-Service.
    admin_token: str | None = Field(
        None,
        description=(
            "Bearer token gating the /admin/bans endpoints. When unset, the "
            "endpoints reject every request with 403 (fail-closed)."
        ),
    )
    bs_internal_bans_url: str | None = Field(
        None,
        description=(
            "Base URL of Backend-Service's /internal/banned-fingerprints CRUD "
            "(BS#1261). Example: "
            "'https://api.wxyc.org/internal/banned-fingerprints'."
        ),
    )
    bs_internal_key: str | None = Field(
        None,
        description=(
            "Shared secret forwarded as X-Internal-Key on calls to BS internal "
            "endpoints (BS#1261 ROM_INTERNAL_KEY). Shared with the request-time "
            "ban-check client (#150)."
        ),
    )

    # Application Metadata
    app_name: str = Field(default="Request-O-Matic", description="Application name")
    app_version: str = Field(default="1.0.0", description="Application version")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # populate_by_name lets fields with a validation_alias (currently just
        # deployment_environment) still be set by their Python field name —
        # required for tests that construct Settings(deployment_environment=...)
        # explicitly, and avoids a mypy "Required dynamic aliases disallowed"
        # complaint from the pydantic plugin.
        populate_by_name=True,
    )


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance.

    Returns:
        Settings: Application settings instance
    """
    return Settings()
