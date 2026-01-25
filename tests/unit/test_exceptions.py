"""Unit tests for custom exceptions."""

from core.exceptions import (
    ArtworkNotFoundError,
    ConfigurationError,
    LibrarySearchError,
    ParseError,
    RequestParserError,
    ServiceInitializationError,
    SlackPostError,
)


def test_base_exception():
    """Test base RequestParserError."""
    exc = RequestParserError("Test error")
    assert str(exc) == "Test error"
    assert exc.details == {}


def test_base_exception_with_details():
    """Test base exception with details dict."""
    details = {"key": "value", "count": 42}
    exc = RequestParserError("Test error", details=details)
    assert exc.details == details


def test_parse_error():
    """Test ParseError exception."""
    exc = ParseError("Failed to parse")
    assert isinstance(exc, RequestParserError)
    assert str(exc) == "Failed to parse"


def test_artwork_not_found_error():
    """Test ArtworkNotFoundError exception."""
    exc = ArtworkNotFoundError("No artwork found")
    assert isinstance(exc, RequestParserError)
    assert str(exc) == "No artwork found"


def test_library_search_error():
    """Test LibrarySearchError exception."""
    exc = LibrarySearchError("Search failed")
    assert isinstance(exc, RequestParserError)
    assert str(exc) == "Search failed"


def test_slack_post_error():
    """Test SlackPostError exception."""
    exc = SlackPostError("Failed to post")
    assert isinstance(exc, RequestParserError)
    assert str(exc) == "Failed to post"


def test_service_initialization_error():
    """Test ServiceInitializationError exception."""
    exc = ServiceInitializationError("Service init failed")
    assert isinstance(exc, RequestParserError)
    assert str(exc) == "Service init failed"


def test_configuration_error():
    """Test ConfigurationError exception."""
    exc = ConfigurationError("Config invalid")
    assert isinstance(exc, RequestParserError)
    assert str(exc) == "Config invalid"


def test_exception_inheritance():
    """Test that all custom exceptions inherit from base."""
    exceptions = [
        ParseError,
        ArtworkNotFoundError,
        LibrarySearchError,
        SlackPostError,
        ServiceInitializationError,
        ConfigurationError,
    ]

    for exc_class in exceptions:
        exc = exc_class("Test")
        assert isinstance(exc, RequestParserError)
        assert isinstance(exc, Exception)
