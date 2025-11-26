"""Application configuration using Pydantic Settings."""
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # API Keys - Required
    groq_api_key: str = Field(..., description="Groq API key for AI parsing")
    
    # API Keys - Optional
    discogs_token: Optional[str] = Field(None, description="Discogs API token for artwork lookup")
    slack_webhook_url: Optional[str] = Field(
        None, description="Slack webhook URL for posting results"
    )
    
    # Database Configuration
    library_db_path: Path = Field(
        default=Path("library.db"), description="Path to SQLite library database"
    )
    
    # Application Configuration
    host: str = Field(default="0.0.0.0", description="Host to bind the server to")
    port: int = Field(default=8000, description="Port to run the server on")
    log_level: str = Field(default="INFO", description="Logging level")
    
    # Feature Flags
    enable_slack_integration: bool = Field(
        default=True, description="Enable Slack notifications"
    )
    enable_artwork_lookup: bool = Field(
        default=True, description="Enable artwork lookup from external APIs"
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


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance.
    
    Returns:
        Settings: Application settings instance
    """
    return Settings()

