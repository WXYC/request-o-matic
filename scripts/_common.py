"""Shared constants and utilities for CLI scripts."""

import logging

PROD_URL = "https://request-o-matic-production.up.railway.app/api/v1"
STAGING_URL = "https://request-o-matic-staging.up.railway.app/api/v1"
LOCAL_URL = "http://localhost:8000/api/v1"


def set_up_logging(verbose: bool, default_level: int = logging.INFO) -> None:
    """Configure logging based on verbosity level.

    Args:
        verbose: If True, use DEBUG level; otherwise use default_level.
        default_level: Logging level when not verbose (default: INFO).
    """
    level = logging.DEBUG if verbose else default_level
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler()],
    )
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
