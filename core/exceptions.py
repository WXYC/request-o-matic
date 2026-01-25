"""Custom exception classes for the request parser application."""


class RequestParserError(Exception):
    """Base exception for all request parser errors."""

    def __init__(self, message: str, details: dict | None = None):
        self.message = message
        self.details = details or {}
        super().__init__(self.message)


class ParseError(RequestParserError):
    """Raised when parsing a request fails."""

    pass


class ArtworkNotFoundError(RequestParserError):
    """Raised when artwork cannot be found for a song/album."""

    pass


class LibrarySearchError(RequestParserError):
    """Raised when a library search operation fails."""

    pass


class SlackPostError(RequestParserError):
    """Raised when posting to Slack fails."""

    pass


class ServiceInitializationError(RequestParserError):
    """Raised when a service fails to initialize."""

    pass


class ConfigurationError(RequestParserError):
    """Raised when there's a configuration error."""

    pass
