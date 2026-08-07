"""Unit tests for services/fingerprint.py.

``normalize_fingerprint`` is the single gate every consumer of the
``X-Device-Fingerprint`` header runs through: the BS ban-check client (so a
malformed value can't provoke a 400 and fail the ban check open) and the Slack
metadata envelope (so request-o-matic#152's "Ban requester" button never
renders against a value ``POST /admin/bans`` will 422).
"""

import pytest

from services.fingerprint import normalize_fingerprint

VALID = [
    "11111111-2222-3333-4444-555555555555",  # any version
    "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",  # uppercase
    "11111111-1111-4111-8111-111111111111",  # v4-shaped
]

INVALID = [
    pytest.param(None, id="absent"),
    pytest.param("", id="empty"),
    pytest.param("   ", id="whitespace-only"),
    pytest.param("\t\n", id="whitespace-control"),
    pytest.param("not-a-uuid", id="malformed"),
    pytest.param("abc?inject=evil", id="query-injection"),
    pytest.param("11111111-1111-4111-8111", id="truncated"),
    pytest.param("11111111-1111-4111-8111-111111111111x", id="trailing-junk"),
    pytest.param("g1111111-1111-4111-8111-111111111111", id="non-hex"),
    pytest.param("x" * 5000, id="oversized"),
]


@pytest.mark.parametrize("value", VALID)
def test_valid_uuid_passes_through(value):
    """Well-formed UUIDs survive unchanged -- case included, since BS's
    UUID_REGEX is case-insensitive and the echoed value must still match the
    device that sent it."""
    assert normalize_fingerprint(value) == value


@pytest.mark.parametrize("value", INVALID)
def test_invalid_value_is_dropped(value):
    """Anything that isn't a UUID collapses to None so callers can treat
    "malformed" and "absent" identically."""
    assert normalize_fingerprint(value) is None


def test_surrounding_whitespace_is_stripped():
    """A padded-but-valid header identifies the same device; keep it rather
    than dropping the ban signal on a formatting quirk."""
    assert normalize_fingerprint("  11111111-1111-4111-8111-111111111111  ") == (
        "11111111-1111-4111-8111-111111111111"
    )
