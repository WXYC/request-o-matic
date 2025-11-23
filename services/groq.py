import logging
import os

from groq import Groq

logger = logging.getLogger(__name__)

_client: Groq | None = None


def init_groq_client() -> Groq:
    """Initialize and return the Groq client."""
    global _client
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        logger.error("GROQ_API_KEY environment variable not set")
        raise RuntimeError("GROQ_API_KEY environment variable not set")
    _client = Groq(api_key=api_key)
    logger.info("Groq client initialized")
    return _client


def get_groq_client() -> Groq:
    """Get the Groq client instance."""
    if _client is None:
        raise RuntimeError("Groq client not initialized")
    return _client
