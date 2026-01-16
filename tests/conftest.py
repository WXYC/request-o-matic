"""Shared test fixtures for pytest."""
import json
import os
import subprocess
from enum import Enum

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from config.settings import Settings
from library.models import LibraryItem


# =============================================================================
# Environment Configuration
# =============================================================================
# Set TEST_ENV to control which server integration tests hit:
#   - local: http://localhost:8000 (default)
#   - staging: https://request-o-matic-staging.up.railway.app
#   - production: https://request-o-matic-production.up.railway.app
# =============================================================================

class TestEnvironment(str, Enum):
    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"


ENVIRONMENT_URLS = {
    TestEnvironment.LOCAL: "http://localhost:8000/api/v1",
    TestEnvironment.STAGING: "https://request-o-matic-staging.up.railway.app/api/v1",
    TestEnvironment.PRODUCTION: "https://request-o-matic-production.up.railway.app/api/v1",
}


def get_test_environment() -> TestEnvironment:
    """Get the test environment from TEST_ENV env var."""
    env_value = os.environ.get("TEST_ENV", "local").lower()
    try:
        return TestEnvironment(env_value)
    except ValueError:
        print(f"⚠ Unknown TEST_ENV '{env_value}', defaulting to 'local'")
        return TestEnvironment.LOCAL


def pytest_configure(config):
    """Load staging environment variables when running integration tests."""
    # Check if we're running integration tests
    markexpr = config.getoption("-m", default="")
    if "integration" not in markexpr:
        return
    
    # Check if RAILWAY_TOKEN_STAGING is set
    token = os.environ.get("RAILWAY_TOKEN_STAGING")
    if not token:
        return  # Will use whatever env vars are already set
    
    try:
        result = subprocess.run(
            ["railway", "variables", "--json"],
            capture_output=True,
            text=True,
            env={**os.environ, "RAILWAY_TOKEN": token},
            timeout=30,
        )
        if result.returncode == 0:
            variables = json.loads(result.stdout)
            for key, value in variables.items():
                os.environ[key] = value
            print(f"\n✓ Loaded {len(variables)} staging environment variables from Railway")
    except FileNotFoundError:
        print("\n⚠ Railway CLI not found. Install with: brew install railway")
    except subprocess.TimeoutExpired:
        print("\n⚠ Timed out fetching Railway variables")
    except json.JSONDecodeError:
        print(f"\n⚠ Failed to parse Railway variables: {result.stdout}")


@pytest.fixture(scope="session")
def test_environment() -> TestEnvironment:
    """Get the current test environment."""
    env = get_test_environment()
    print(f"\n✓ Test environment: {env.value}")
    return env


@pytest.fixture(scope="session")
def base_url(test_environment: TestEnvironment) -> str:
    """Get the base URL for the current test environment."""
    url = ENVIRONMENT_URLS[test_environment]
    print(f"✓ Base URL: {url}")
    return url


@pytest.fixture
def test_settings():
    """Create test settings with mock values."""
    return Settings(
        groq_api_key="test_groq_key",
        discogs_token="test_discogs_token",
        slack_webhook_url="https://hooks.slack.com/test",
        library_db_path=Path("test_library.db"),
        log_level="DEBUG",
        enable_slack_integration=False,  # Disable for most tests
        enable_artwork_lookup=True,
    )


@pytest.fixture
def mock_groq_client():
    """Create a mock Groq client."""
    client = Mock()
    client.chat = Mock()
    client.chat.completions = Mock()
    client.chat.completions.create = AsyncMock()
    return client


@pytest.fixture
def mock_library_db():
    """Create a mock library database."""
    db = AsyncMock()
    db.search = AsyncMock(return_value=[])
    db.connect = AsyncMock()
    db.close = AsyncMock()
    db._conn = Mock()  # Mock connection object
    return db


@pytest.fixture
def mock_artwork_finder():
    """Create a mock artwork finder."""
    finder = AsyncMock()
    finder.find = AsyncMock(return_value=None)
    finder.providers = []
    return finder


@pytest.fixture
def mock_slack_service():
    """Create a mock Slack service."""
    service = AsyncMock()
    service.post_blocks = AsyncMock()
    service.webhook_url = "https://hooks.slack.com/test"
    return service


@pytest.fixture
def sample_library_item():
    """Create a sample library item for testing."""
    return LibraryItem(
        id=1,
        artist="Queen",
        title="A Night at the Opera",
        call_letters="Q",
        artist_call_number=1,
        release_call_number=1,
        genre="Rock",
        format="CD",
    )


@pytest.fixture
def sample_library_items():
    """Create multiple sample library items for testing."""
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


@pytest.fixture
def sample_parsed_request():
    """Create a sample parsed request for testing."""
    from services.parser import ParsedRequest, MessageType
    
    return ParsedRequest(
        song="Bohemian Rhapsody",
        album="A Night at the Opera",
        artist="Queen",
        is_request=True,
        message_type=MessageType.REQUEST,
        raw_message="Play Bohemian Rhapsody by Queen",
    )

