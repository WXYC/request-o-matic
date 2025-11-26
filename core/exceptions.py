"""Custom exception classes for the request parser application."""


class RequestParserException(Exception):
    """Base exception for all request parser errors."""

    def __init__(self, message: str, details: dict = None):
        self.message = message
        self.details = details or {}
        super().__init__(self.message)


class ParseError(RequestParserException):
    """Raised when parsing a request fails."""

    pass


class ArtworkNotFoundError(RequestParserException):
    """Raised when artwork cannot be found for a song/album."""

    pass


class LibrarySearchError(RequestParserException):
    """Raised when a library search operation fails."""

    pass


class SlackPostError(RequestParserException):
    """Raised when posting to Slack fails."""

    pass


class ServiceInitializationError(RequestParserException):
    """Raised when a service fails to initialize."""

    pass


class ConfigurationError(RequestParserException):
    """Raised when there's a configuration error."""

    pass

