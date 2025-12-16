"""Shared test fixtures for pytest."""
import json
import os
import subprocess

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from config.settings import Settings
from library.models import LibraryItem


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

