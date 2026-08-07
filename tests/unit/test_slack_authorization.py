"""Unit tests for services/slack_authorization.py -- the Ban button allowlist
(request-o-matic#152).

``SLACK_BAN_AUTHORIZED_USERS`` is comma-separated. Unset or empty means
deny-all: a deploy that drops the variable must disable the button for
everyone, not open it to the whole workspace.
"""

from __future__ import annotations

import pytest

from services.slack_authorization import is_authorized_slack_user


class TestEmptyAllowlistDeniesAll:
    def test_none_allowlist_denies(self):
        assert is_authorized_slack_user("U01ABC", None) is False

    def test_empty_string_allowlist_denies(self):
        assert is_authorized_slack_user("U01ABC", "") is False

    def test_whitespace_only_allowlist_denies(self):
        assert is_authorized_slack_user("U01ABC", "   ") is False

    def test_commas_only_allowlist_denies(self):
        """A value that parses to zero usable entries (e.g. a stray comma)
        must deny-all exactly like an unset variable, not silently admit
        an empty-string 'user'."""
        assert is_authorized_slack_user("U01ABC", ",,") is False


class TestAllowlistedUser:
    def test_single_entry_match(self):
        assert is_authorized_slack_user("U01ABC", "U01ABC") is True

    def test_matches_among_multiple_entries(self):
        assert is_authorized_slack_user("U02DEF", "U01ABC,U02DEF,U03GHI") is True

    def test_tolerates_whitespace_around_entries(self):
        assert is_authorized_slack_user("U02DEF", "U01ABC, U02DEF , U03GHI") is True


class TestUnauthorizedUser:
    def test_user_not_in_allowlist_denies(self):
        assert is_authorized_slack_user("U99ZZZ", "U01ABC,U02DEF") is False

    def test_case_sensitive_match(self):
        """Slack user IDs are case-sensitive; a lowercase match must not
        pass -- coercing case would let a near-miss silently through."""
        assert is_authorized_slack_user("u01abc", "U01ABC") is False

    def test_none_user_id_denies(self):
        assert is_authorized_slack_user(None, "U01ABC,U02DEF") is False

    def test_empty_user_id_denies(self):
        assert is_authorized_slack_user("", "U01ABC,U02DEF") is False

    @pytest.mark.parametrize(
        "user_id",
        [["U01ABC"], {"id": "U01ABC"}, {"U01ABC"}, ("U01ABC",), 0, 1, False, True, 1.5],
        ids=[
            "list",
            "dict",
            "set",
            "tuple",
            "int-zero",
            "int",
            "bool-false",
            "bool-true",
            "float",
        ],
    )
    def test_non_string_user_id_denies_without_raising(self, user_id):
        """Both call sites read this out of ``json.loads`` output, which is
        typed ``Any``. An unhashable value reaching the ``in`` test raises
        TypeError, and on this route an uncaught exception is a 500 whose
        Sentry event carries the settings object -- so shape has to fail
        closed the same way value does.
        """
        assert is_authorized_slack_user(user_id, "U01ABC,U02DEF") is False
