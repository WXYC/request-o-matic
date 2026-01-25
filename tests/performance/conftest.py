"""Performance test fixtures - ensures local server is running for local tests."""

import pytest


@pytest.fixture(autouse=True)
def ensure_server_running(local_server):
    """Ensure the local server fixture runs before any performance test.

    This fixture is auto-used, so performance tests will automatically
    start the local server when TEST_ENV=local.
    """
    pass
