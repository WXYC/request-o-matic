"""Unit tests for services/slack_authorization.py -- the Ban button allowlist
(request-o-matic#152) and the stored-roster union (request-o-matic#240).

``SLACK_BAN_AUTHORIZED_USERS`` is comma-separated. Unset or empty means
deny-all: a deploy that drops the variable must disable the button for
everyone, not open it to the whole workspace.

Since #240 the authorized set is the *union* of that environment allowlist and
a roster stored in Backend-Service. The classes below the original ones cover
the union; the original ones are now doing double duty as the regression test
that extracting ``is_authorized_slack_user_in`` did not move the fail-closed
guarantee out of ``is_authorized_slack_user``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from services.moderator_client import ModeratorClientError
from services.slack_authorization import (
    is_authorized_slack_user,
    is_authorized_slack_user_in,
    resolve_authorized_users,
)


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


class TestIsAuthorizedSlackUserIn:
    """The extracted set-taking predicate (#240).

    ``is_authorized_slack_user`` cannot test membership against a computed set
    without re-serializing it back to CSV, so the union needs a predicate that
    takes the set directly. The fail-closed shape guard lives here now, and the
    CSV wrapper delegates to it -- these tests pin that the guard survived the
    move.
    """

    def test_member_is_authorized(self):
        assert is_authorized_slack_user_in("U01ABC", frozenset({"U01ABC", "U02DEF"})) is True

    def test_non_member_denied(self):
        assert is_authorized_slack_user_in("U99ZZZ", frozenset({"U01ABC"})) is False

    def test_empty_set_denies_all(self):
        assert is_authorized_slack_user_in("U01ABC", frozenset()) is False

    def test_none_user_id_denies(self):
        assert is_authorized_slack_user_in(None, frozenset({"U01ABC"})) is False

    def test_empty_user_id_denies(self):
        assert is_authorized_slack_user_in("", frozenset({"U01ABC"})) is False

    def test_case_sensitive(self):
        assert is_authorized_slack_user_in("u01abc", frozenset({"U01ABC"})) is False

    @pytest.mark.parametrize(
        "user_id",
        [["U01ABC"], {"id": "U01ABC"}, {"U01ABC"}, ("U01ABC",), 0, 1, False, True, 1.5],
        ids=["list", "dict", "set", "tuple", "int-zero", "int", "bool-false", "bool-true", "float"],
    )
    def test_non_string_user_id_denies_without_raising(self, user_id):
        """The unhashable-value TypeError guard, at its new home."""
        assert is_authorized_slack_user_in(user_id, frozenset({"U01ABC"})) is False


def _roster_client(*user_ids: str) -> AsyncMock:
    client = AsyncMock()
    client.list_moderators = AsyncMock(return_value=list(user_ids))
    return client


def _failing_client(status_code: int = 0) -> AsyncMock:
    client = AsyncMock()
    client.list_moderators = AsyncMock(
        side_effect=ModeratorClientError(status_code, {"error": "upstream_unreachable"})
    )
    return client


class TestResolveAuthorizedUsers:
    """The union of the environment allowlist and the stored roster (#240)."""

    @pytest.mark.asyncio
    async def test_returns_the_union(self):
        result = await resolve_authorized_users(_roster_client("U03GHI"), "U01ABC,U02DEF")
        assert result == frozenset({"U01ABC", "U02DEF", "U03GHI"})

    @pytest.mark.asyncio
    async def test_empty_table_yields_the_env_allowlist_alone(self):
        """Day one: the table ships empty and the env allowlist still bans."""
        result = await resolve_authorized_users(_roster_client(), "U01ABC,U02DEF")
        assert result == frozenset({"U01ABC", "U02DEF"})

    @pytest.mark.asyncio
    async def test_empty_env_allowlist_yields_the_table_alone(self):
        """After the break-glass trim, the table is doing the work."""
        result = await resolve_authorized_users(_roster_client("U03GHI"), None)
        assert result == frozenset({"U03GHI"})

    @pytest.mark.asyncio
    async def test_both_empty_yields_empty_set(self):
        assert await resolve_authorized_users(_roster_client(), None) == frozenset()

    @pytest.mark.asyncio
    async def test_overlap_is_deduplicated(self):
        result = await resolve_authorized_users(_roster_client("U01ABC"), "U01ABC")
        assert result == frozenset({"U01ABC"})


class TestResolveAuthorizedUsersFailsClosed:
    """An unreachable Backend-Service must SHRINK the authorized set."""

    @pytest.mark.asyncio
    async def test_client_error_falls_back_to_env_allowlist(self):
        result = await resolve_authorized_users(_failing_client(), "U01ABC,U02DEF")
        assert result == frozenset({"U01ABC", "U02DEF"})

    @pytest.mark.asyncio
    async def test_none_client_falls_back_identically(self):
        """An unconfigured upstream is treated exactly like an unreachable one.

        ``get_moderator_client`` returns None when BS_INTERNAL_MODERATORS_URL
        or BS_INTERNAL_KEY is unset, rather than raising 503 like its sibling:
        a 503 out of the authorization path would take the ban button down
        along with the roster.
        """
        result = await resolve_authorized_users(None, "U01ABC,U02DEF")
        assert result == frozenset({"U01ABC", "U02DEF"})

    @pytest.mark.asyncio
    async def test_failure_never_widens_the_authorized_set(self):
        """The explicit regression test for the fail-closed posture.

        A moderator who exists only in the stored roster must lose access when
        Backend-Service is unreachable. The failure mode to prevent is the
        opposite one -- an upstream error being read as "allow", which would
        turn a BS outage into workspace-wide ban rights.
        """
        stored_only = "U03GHI"

        healthy = await resolve_authorized_users(_roster_client(stored_only), "U01ABC")
        assert is_authorized_slack_user_in(stored_only, healthy) is True

        degraded = await resolve_authorized_users(_failing_client(), "U01ABC")
        assert is_authorized_slack_user_in(stored_only, degraded) is False
        assert degraded == frozenset({"U01ABC"})

    @pytest.mark.asyncio
    async def test_client_error_with_both_halves_empty_denies_all(self):
        assert await resolve_authorized_users(_failing_client(), None) == frozenset()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [0, 401, 500], ids=["transport", "401", "500"])
    async def test_any_client_error_falls_back(self, status_code):
        result = await resolve_authorized_users(_failing_client(status_code), "U01ABC")
        assert result == frozenset({"U01ABC"})


class TestResolveAuthorizedUsersCaseHandling:
    """The union is deliberately asymmetric on case."""

    @pytest.mark.asyncio
    async def test_env_allowlist_entries_are_not_uppercased(self):
        """Backend-Service normalizes the roster to uppercase; that is storage-side
        only. The environment half is unioned verbatim, preserving the documented
        case-sensitive comparison. Uppercasing it here would silently change a
        shipped security contract to rescue hand-typed lowercase entries that are
        already broken today and should be fixed in the variable instead.
        """
        result = await resolve_authorized_users(_roster_client(), "u01abc")

        assert result == frozenset({"u01abc"})
        assert is_authorized_slack_user_in("U01ABC", result) is False

    @pytest.mark.asyncio
    async def test_stored_roster_arrives_uppercased_and_is_used_as_given(self):
        result = await resolve_authorized_users(_roster_client("U03GHI"), None)
        assert is_authorized_slack_user_in("U03GHI", result) is True
