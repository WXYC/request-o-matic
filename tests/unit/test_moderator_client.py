"""Unit tests for services/moderator_client.py (request-o-matic#240).

Donor: tests/unit/test_ban_admin_client.py. The structural mirror is
deliberate -- both clients talk to a key-gated Backend-Service `/internal`
endpoint with the same error-envelope contract.

The one place this file deliberately diverges from its donor is timeouts.
Every call this client makes runs inside a Slack deadline, so the sibling's
10s default would blow the `trigger_id` window rather than merely being slow.
Those assertions are regression tests, not incidental coverage.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from services.moderator_client import (
    MODERATOR_READ_TIMEOUT_SECONDS,
    MODERATOR_WRITE_TIMEOUT_SECONDS,
    ModeratorClient,
    ModeratorClientError,
)

BS_BASE = "https://bs.example.com/internal/slack-ban-moderators"
INTERNAL_KEY = "test-internal-key"

U_ONE = "U01ABCDEF"
U_TWO = "U02GHIJKL"
U_THREE = "U03MNOPQR"


def _client(http_client: object) -> ModeratorClient:
    """Build a ModeratorClient against the supplied (Async)Mock."""
    return ModeratorClient(BS_BASE, http_client, internal_key=INTERNAL_KEY)  # type: ignore[arg-type]


def _response(status_code: int, body: object | None = None) -> Mock:
    resp = Mock()
    resp.status_code = status_code
    if body is None:
        resp.json = Mock(side_effect=ValueError("no json"))
        resp.text = "<html>gateway</html>"
    else:
        resp.json = Mock(return_value=body)
        resp.text = str(body)
    return resp


def _items(*user_ids: str) -> dict[str, object]:
    """A BS GET/PUT success envelope: {"items": [row, ...]}."""
    return {
        "items": [
            {
                "slack_user_id": user_id,
                "added_at": "2026-01-01T00:00:00.000Z",
                "added_by_slack_user_id": None,
            }
            for user_id in user_ids
        ]
    }


class TestListModerators:
    """GET / -> {"items": [...]}."""

    @pytest.mark.asyncio
    async def test_returns_ids_in_upstream_order(self):
        """The roster is returned as IDs, preserving BS's ORDER BY.

        BS sorts by (added_at, slack_user_id) and that order is load-bearing:
        it is what keeps the modal's `initial_users` from flapping between
        renders of an unchanged roster.
        """
        http = AsyncMock()
        http.get = AsyncMock(return_value=_response(200, _items(U_TWO, U_ONE, U_THREE)))

        assert await _client(http).list_moderators() == [U_TWO, U_ONE, U_THREE]

    @pytest.mark.asyncio
    async def test_sends_x_internal_key_header(self):
        http = AsyncMock()
        http.get = AsyncMock(return_value=_response(200, _items(U_ONE)))

        await _client(http).list_moderators()

        called_args, called_kwargs = http.get.call_args
        assert called_args[0] == BS_BASE
        assert called_kwargs["headers"] == {"X-Internal-Key": INTERNAL_KEY}

    @pytest.mark.asyncio
    async def test_empty_roster_is_not_an_error(self):
        """An empty table is a legal state, not a failure.

        It must return [] rather than raising, because raising would be
        indistinguishable from an outage at the call site and would make
        resolve_authorized_users log a fallback that never happened.
        """
        http = AsyncMock()
        http.get = AsyncMock(return_value=_response(200, {"items": []}))

        assert await _client(http).list_moderators() == []

    @pytest.mark.asyncio
    async def test_read_uses_the_slack_deadline_not_the_siblings_default(self):
        """The read is bounded at 1.5s, NOT BanAdminClient's 10.0s.

        Regression test for the whole reason this client exists separately.
        This read happens inside Slack's 3-second `trigger_id` window with a
        `views.open` still to follow it, so a 10s timeout would guarantee
        `expired_trigger_id` rather than merely being generous.
        """
        http = AsyncMock()
        http.get = AsyncMock(return_value=_response(200, _items(U_ONE)))

        await _client(http).list_moderators()

        assert http.get.call_args[1]["timeout"] == MODERATOR_READ_TIMEOUT_SECONDS
        assert MODERATOR_READ_TIMEOUT_SECONDS == 1.5

    @pytest.mark.asyncio
    async def test_timeout_raises_rather_than_hanging(self):
        """A slow upstream surfaces as ModeratorClientError(status_code=0).

        This is what lets the caller fall back to the environment allowlist
        instead of holding a worker past the point the response could be used.
        """
        http = AsyncMock()
        http.get = AsyncMock(side_effect=httpx.TimeoutException("read timed out"))

        with pytest.raises(ModeratorClientError) as excinfo:
            await _client(http).list_moderators()

        assert excinfo.value.status_code == 0
        assert excinfo.value.body["error"] == "upstream_unreachable"

    @pytest.mark.asyncio
    async def test_transport_failure_maps_to_status_zero(self):
        http = AsyncMock()
        http.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

        with pytest.raises(ModeratorClientError) as excinfo:
            await _client(http).list_moderators()

        assert excinfo.value.status_code == 0

    @pytest.mark.asyncio
    async def test_non_json_2xx_raises(self):
        """A 200 carrying HTML (reverse proxy, content-type drift) is an error.

        Without this guard the bare .json() would raise ValueError and escape
        the caller's `except ModeratorClientError` as an unhandled 500.
        """
        http = AsyncMock()
        http.get = AsyncMock(return_value=_response(200, None))

        with pytest.raises(ModeratorClientError) as excinfo:
            await _client(http).list_moderators()

        assert excinfo.value.body["error"] == "non_json_upstream_body"

    @pytest.mark.asyncio
    async def test_401_surfaces_faithfully(self):
        http = AsyncMock()
        http.get = AsyncMock(return_value=_response(401, {"error": "Unauthorized"}))

        with pytest.raises(ModeratorClientError) as excinfo:
            await _client(http).list_moderators()

        assert excinfo.value.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"items": "not-a-list"},
            {"items": [{"added_at": "2026-01-01T00:00:00.000Z"}]},
            {"items": [{"slack_user_id": 12345}]},
            {"items": [None]},
        ],
        ids=[
            "missing-items",
            "items-not-a-list",
            "row-missing-id",
            "id-not-a-string",
            "row-is-null",
        ],
    )
    async def test_malformed_success_shape_raises(self, body):
        """A 200 whose shape isn't the contract fails closed.

        This list authorizes a privileged action, so an unreadable response
        must raise -- letting the caller shrink to the break-glass allowlist --
        rather than degrading to a partial or empty roster that would look
        like a legitimately empty table.
        """
        http = AsyncMock()
        http.get = AsyncMock(return_value=_response(200, body))

        with pytest.raises(ModeratorClientError):
            await _client(http).list_moderators()


class TestReplaceModerators:
    """PUT / with {slackUserIds, expectedCurrent, actorSlackUserId?}."""

    @pytest.mark.asyncio
    async def test_sends_the_bs_camelcase_contract(self):
        http = AsyncMock()
        http.put = AsyncMock(return_value=_response(200, _items(U_ONE, U_TWO)))

        await _client(http).replace_moderators(
            slack_user_ids=[U_ONE, U_TWO],
            expected_current=[U_ONE],
            actor_slack_user_id=U_THREE,
        )

        called_args, called_kwargs = http.put.call_args
        assert called_args[0] == BS_BASE
        assert called_kwargs["json"] == {
            "slackUserIds": [U_ONE, U_TWO],
            "expectedCurrent": [U_ONE],
            "actorSlackUserId": U_THREE,
        }
        assert called_kwargs["headers"] == {"X-Internal-Key": INTERNAL_KEY}

    @pytest.mark.asyncio
    async def test_omits_actor_when_absent(self):
        """actorSlackUserId is optional; send no key rather than an explicit null."""
        http = AsyncMock()
        http.put = AsyncMock(return_value=_response(200, _items(U_ONE)))

        await _client(http).replace_moderators(slack_user_ids=[U_ONE], expected_current=[])

        assert "actorSlackUserId" not in http.put.call_args[1]["json"]

    @pytest.mark.asyncio
    async def test_empty_roster_is_a_legal_write(self):
        """Removing everyone is a legal edit and must not be special-cased away."""
        http = AsyncMock()
        http.put = AsyncMock(return_value=_response(200, {"items": []}))

        assert (
            await _client(http).replace_moderators(slack_user_ids=[], expected_current=[U_ONE])
            == []
        )
        assert http.put.call_args[1]["json"]["slackUserIds"] == []

    @pytest.mark.asyncio
    async def test_returns_the_resulting_roster(self):
        http = AsyncMock()
        http.put = AsyncMock(return_value=_response(200, _items(U_ONE, U_TWO)))

        result = await _client(http).replace_moderators(
            slack_user_ids=[U_ONE, U_TWO], expected_current=[U_ONE]
        )

        assert result == [U_ONE, U_TWO]

    @pytest.mark.asyncio
    async def test_write_uses_the_view_submission_deadline(self):
        """The write is bounded at 2.5s, inside Slack's 3s view_submission window."""
        http = AsyncMock()
        http.put = AsyncMock(return_value=_response(200, _items(U_ONE)))

        await _client(http).replace_moderators(slack_user_ids=[U_ONE], expected_current=[])

        assert http.put.call_args[1]["timeout"] == MODERATOR_WRITE_TIMEOUT_SECONDS
        assert MODERATOR_WRITE_TIMEOUT_SECONDS == 2.5

    @pytest.mark.asyncio
    async def test_409_surfaces_faithfully_with_its_body(self):
        """A concurrent-edit 409 must reach the caller intact.

        The router renders it as a Slack `response_action: "errors"`, and the
        `current` list in the body is what tells the submitter what the roster
        actually holds now -- so both the status code and the body have to
        survive the trip.
        """
        conflict = {
            "error": "Moderator roster changed since it was read; re-open the picker and try again",
            "current": [U_ONE, U_THREE],
        }
        http = AsyncMock()
        http.put = AsyncMock(return_value=_response(409, conflict))

        with pytest.raises(ModeratorClientError) as excinfo:
            await _client(http).replace_moderators(slack_user_ids=[U_TWO], expected_current=[U_ONE])

        assert excinfo.value.status_code == 409
        assert excinfo.value.body["current"] == [U_ONE, U_THREE]

    @pytest.mark.asyncio
    async def test_400_surfaces_faithfully(self):
        http = AsyncMock()
        http.put = AsyncMock(
            return_value=_response(
                400, {"error": "slackUserIds must be an array of Slack user IDs"}
            )
        )

        with pytest.raises(ModeratorClientError) as excinfo:
            await _client(http).replace_moderators(slack_user_ids=[], expected_current=[])

        assert excinfo.value.status_code == 400

    @pytest.mark.asyncio
    async def test_transport_failure_maps_to_status_zero(self):
        http = AsyncMock()
        http.put = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

        with pytest.raises(ModeratorClientError) as excinfo:
            await _client(http).replace_moderators(slack_user_ids=[U_ONE], expected_current=[])

        assert excinfo.value.status_code == 0


class TestWireNormalization:
    """Both roster lists are uppercased, de-duplicated, and sorted before sending.

    BS applies the same normalization on write and on compare, and documents
    that ROM normalizes first. Doing it here is what keeps a case-only or
    order-only edit from depending on the far side to avoid a spurious 409.
    """

    def test_uppercases_dedupes_and_sorts(self):
        from services.moderator_client import normalize_slack_user_ids

        assert normalize_slack_user_ids(["u02ghijkl", "U01ABCDEF", "U02GHIJKL"]) == [
            "U01ABCDEF",
            "U02GHIJKL",
        ]

    def test_empty_stays_empty(self):
        from services.moderator_client import normalize_slack_user_ids

        assert normalize_slack_user_ids([]) == []

    @pytest.mark.asyncio
    async def test_case_only_edit_sends_matching_lists(self):
        """A case-differing selection must not manufacture a conflict.

        Slack sends IDs uppercase, so this is defense against a hand-built or
        replayed payload rather than a routine case -- but it is exactly the
        kind of difference that would otherwise read as "someone else edited
        the roster" and reject a save that changed nothing.
        """
        http = AsyncMock()
        http.put = AsyncMock(return_value=_response(200, _items(U_ONE)))

        await _client(http).replace_moderators(
            slack_user_ids=[U_ONE.lower()], expected_current=[U_ONE]
        )

        sent = http.put.call_args[1]["json"]
        assert sent["slackUserIds"] == sent["expectedCurrent"] == [U_ONE]

    @pytest.mark.asyncio
    async def test_ordering_differences_do_not_survive_to_the_wire(self):
        http = AsyncMock()
        http.put = AsyncMock(return_value=_response(200, _items(U_ONE, U_TWO)))

        await _client(http).replace_moderators(
            slack_user_ids=[U_TWO, U_ONE], expected_current=[U_ONE, U_TWO]
        )

        sent = http.put.call_args[1]["json"]
        assert sent["slackUserIds"] == [U_ONE, U_TWO]
        assert sent["expectedCurrent"] == [U_ONE, U_TWO]
