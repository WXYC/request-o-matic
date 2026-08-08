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
    slack_bot_token: str | None = Field(
        None, description="Slack bot token for chat.postMessage (request-o-matic#215)"
    )
    slack_channel_id: str | None = Field(
        None, description="Slack channel ID to post to via chat.postMessage (request-o-matic#215)"
    )
    slack_use_bot_token: bool = Field(
        default=False,
        description=(
            "Feature flag: post via chat.postMessage with SLACK_BOT_TOKEN instead of the "
            "incoming webhook (request-o-matic#215). Default False; the webhook path stays "
            "byte-for-byte unchanged while this is off."
        ),
    )
    slack_signing_secret: str | None = Field(
        None,
        description=(
            "Slack app signing secret used to verify the X-Slack-Signature HMAC on "
            "inbound POST /slack/interactivity callbacks (request-o-matic#152). "
            "Distinct from SLACK_BOT_TOKEN -- this never leaves rom, it only verifies "
            "requests coming in from Slack."
        ),
    )
    slack_ban_authorized_users: str | None = Field(
        None,
        description=(
            "Comma-separated Slack user IDs allowed to ban a requester via the "
            "in-Slack 'Ban requester' button (request-o-matic#152), e.g. "
            "'U01ABC,U02DEF'. Unset or empty means deny-all: fail closed rather than "
            "letting a dropped env var open banning to the whole workspace."
        ),
    )

    # Application Configuration
    host: str = Field(default="0.0.0.0", description="Host to bind the server to")
    port: int = Field(default=8000, description="Port to run the server on")
    log_level: str = Field(default="INFO", description="Logging level")

    # Feature Flags
    enable_slack_integration: bool = Field(default=True, description="Enable Slack notifications")
    enable_telemetry: bool = Field(default=True, description="Enable PostHog telemetry")
    enable_server_timing: bool = Field(
        default=True,
        description=(
            "Emit a Server-Timing response header on /request that merges rom's own "
            "per-stage telemetry (parse, lookup_service, slack_post) with the sub-stage "
            "breakdown LML forwards in its Server-Timing header (Backend-Service#881). "
            "Purely additive/out-of-band; the JSON body is unchanged. Set "
            "ENABLE_SERVER_TIMING=false to disable (the Railway kill switch)."
        ),
    )

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
    bs_internal_moderators_url: str | None = Field(
        None,
        description=(
            "Base URL of Backend-Service's /internal/slack-ban-moderators roster "
            "(BS#2045). Example: "
            "'https://api.wxyc.org/internal/slack-ban-moderators'. Reuses "
            "BS_INTERNAL_KEY -- no second secret. Unset means no roster client "
            "is built; unlike BS_INTERNAL_BANS_URL that is a degraded mode "
            "rather than a 503, because this client is read on the ban-button "
            "authorization path -- ban authorization falls back to "
            "SLACK_BAN_AUTHORIZED_USERS alone."
        ),
    )
    bs_internal_key: str | None = Field(
        None,
        description=(
            "Shared secret forwarded as X-Internal-Key on calls to BS internal "
            "endpoints (BS#1261 ROM_INTERNAL_KEY). Used by /admin/bans (#151) "
            "and the moderator roster (#240); the public /auth/check-request-ban "
            "handler does NOT consume this."
        ),
    )

    # Request-line ban enforcement (WXYC/request-o-matic#150 + WXYC/Backend-Service#1261).
    # When enforce_request_bans is True and the caller supplies Authorization
    # and/or X-Device-Fingerprint, ROM consults BS's /auth/check-request-ban
    # endpoint and blocks 403 if BS says banned. See docs/architecture.md for
    # the full flow.
    bs_check_request_ban_url: str | None = Field(
        None,
        description=(
            "Full URL of Backend-Service's POST /auth/check-request-ban endpoint "
            "(apps/auth service, port 8082). When unset, the ban check is disabled "
            "regardless of enforce_request_bans."
        ),
    )
    enforce_request_bans: bool = Field(
        default=False,
        description=(
            "Feature flag for request-line ban enforcement. Default False so the "
            "code can deploy before iOS 3.2 reaches App Store rollout."
        ),
    )

    # User-Agent gate for known-client strict mode (WXYC/request-o-matic#155).
    # When True, requests whose User-Agent identifies a registered WXYC client
    # at-or-above its strict-mode version (currently WXYC-iOS >= 3.2) are
    # rejected 403 if X-Device-Fingerprint is absent or malformed (it is
    # normalized through services/fingerprint.normalize_fingerprint, so a
    # non-UUID is treated exactly like a missing header). Unknown UAs (curl,
    # browsers, v3.1 iOS, etc.) are unaffected. Independent of
    # enforce_request_bans: this gate is a structural requirement on known
    # clients, not a ban decision.
    strict_fingerprint_for_known_clients: bool = Field(
        default=False,
        description=(
            "Feature flag for the User-Agent gate (WXYC/request-o-matic#155). "
            "When True, known WXYC clients (iOS >= 3.2) must send a well-formed "
            "X-Device-Fingerprint UUID or the request is rejected 403."
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
