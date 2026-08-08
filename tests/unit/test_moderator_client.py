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
    MAX_ROSTER_SIZE,
    MODERATOR_READ_BUDGET_SECONDS,
    MODERATOR_READ_TIMEOUT,
    MODERATOR_WRITE_BUDGET_SECONDS,
    MODERATOR_WRITE_TIMEOUT,
    ModeratorClient,
    ModeratorClientError,
    normalize_slack_user_ids,
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


def _budget(timeout: httpx.Timeout) -> float:
    """Worst-case wall clock of an httpx.Timeout: the sum of its phases.

    httpx applies connect/read/write/pool independently, so a bare float is
    four bounds rather than one. Every deadline claim in this module is about
    this number.
    """
    phases = [timeout.connect, timeout.read, timeout.write, timeout.pool]
    assert all(p is not None for p in phases), "an unset phase inherits the client's 30s"
    return sum(p for p in phases if p is not None)


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

        assert http.get.call_args[1]["timeout"] == MODERATOR_READ_TIMEOUT
        assert _budget(MODERATOR_READ_TIMEOUT) == pytest.approx(MODERATOR_READ_BUDGET_SECONDS)

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

        assert http.put.call_args[1]["timeout"] == MODERATOR_WRITE_TIMEOUT
        assert _budget(MODERATOR_WRITE_TIMEOUT) == pytest.approx(MODERATOR_WRITE_BUDGET_SECONDS)

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


class TestRosterSizeCeiling:
    """The one shape that fails *open* (#240 review).

    Every ID in a roster response is unioned into the set that may ban a
    listener. `_extract_ids`'s other guards all fail closed -- a malformed row
    shrinks the set to break-glass. An unbounded list is the exception: a
    Backend-Service regression dropping the WHERE clause hands rom every user
    row, and without a ceiling rom grants all of them ban rights with nothing
    raised and nothing logged.
    """

    @pytest.mark.asyncio
    async def test_a_roster_at_the_ceiling_is_accepted(self):
        rows = [{"slack_user_id": f"U{i:09d}"} for i in range(MAX_ROSTER_SIZE)]
        http = AsyncMock()
        http.get = AsyncMock(return_value=_response(200, {"items": rows}))

        assert len(await _client(http).list_moderators()) == MAX_ROSTER_SIZE

    @pytest.mark.asyncio
    async def test_one_row_over_the_ceiling_is_refused(self):
        rows = [{"slack_user_id": f"U{i:09d}"} for i in range(MAX_ROSTER_SIZE + 1)]
        http = AsyncMock()
        http.get = AsyncMock(return_value=_response(200, {"items": rows}))

        with pytest.raises(ModeratorClientError) as excinfo:
            await _client(http).list_moderators()
        assert excinfo.value.body["error"] == "roster_too_large"

    @pytest.mark.asyncio
    async def test_a_runaway_roster_refuses_rather_than_widening(self):
        """The failure this exists for, at the scale it would actually arrive."""
        rows = [{"slack_user_id": f"U{i:09d}"} for i in range(50_000)]
        http = AsyncMock()
        http.get = AsyncMock(return_value=_response(200, {"items": rows}))

        with pytest.raises(ModeratorClientError):
            await _client(http).list_moderators()


class TestEveryFailureFunnelsIntoModeratorClientError:
    """The client's contract, which callers' fail-closed logic depends on.

    `resolve_authorized_users` catches `ModeratorClientError` and nothing else.
    Anything that escapes is a 500 on the ban-authorization path -- and since
    Sentry captures frame locals by default, that 500 ships the settings object.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_url",
        [
            "https://api.wxyc.org/internal/slack-ban-moderators\n",
            "https://api.wxyc.org:8080x/internal/slack-ban-moderators",
        ],
        ids=["trailing-newline", "bad-port"],
    )
    async def test_a_malformed_base_url_is_contained(self, bad_url):
        """`httpx.InvalidURL` is NOT an `httpx.HTTPError` -- it escaped the
        funnel until the `except` was widened.

        This is the realistic operator error: a URL pasted into the Railway
        dashboard with a trailing newline. Note the inversion it used to cause
        -- *forgetting* the variable degraded gracefully, while *setting it
        wrong* took the ban button down.
        """
        async with httpx.AsyncClient() as real:
            client = ModeratorClient(bad_url, real, internal_key=INTERNAL_KEY)
            with pytest.raises(ModeratorClientError):
                await client.list_moderators()

    def test_normalize_rejects_non_strings_rather_than_attribute_erroring(self):
        """`normalize_slack_user_ids` is the module's only public helper; a bare
        `AttributeError` out of it would break the same contract."""
        with pytest.raises(ModeratorClientError):
            normalize_slack_user_ids([None])  # type: ignore[list-item]
        with pytest.raises(ModeratorClientError):
            normalize_slack_user_ids(["U01ABC", ""])


class TestExtractIdsUncoveredShapes:
    """Branches the original suite never reached (#240 review)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload",
        [[{"slack_user_id": "U01ABC"}], "U01ABC", 42, None],
        ids=["top-level-list", "bare-string", "int", "null"],
    )
    async def test_non_dict_payload_is_refused(self, payload):
        """A reverse proxy or a version-skewed BS can return any of these."""
        http = AsyncMock()
        http.get = AsyncMock(return_value=_response(200, payload))

        with pytest.raises(ModeratorClientError):
            await _client(http).list_moderators()

    @pytest.mark.asyncio
    async def test_empty_string_user_id_is_refused(self):
        """`""` must never enter a set that authorizes anything."""
        http = AsyncMock()
        http.get = AsyncMock(return_value=_response(200, {"items": [{"slack_user_id": ""}]}))

        with pytest.raises(ModeratorClientError):
            await _client(http).list_moderators()
