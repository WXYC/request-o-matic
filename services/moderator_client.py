"""HTTP client for Backend-Service's Slack ban-moderator roster (BS#2045).

This is the *only* place in rom that talks to BS's
`/internal/slack-ban-moderators` endpoints. It is a structural mirror of
`services/ban_admin_client.py` -- same `X-Internal-Key` header, same
`{status_code, body}` error envelope, same non-JSON-2xx guard -- because both
speak to a key-gated BS `/internal` surface and a reader who knows one should
recognize the other.

Response shapes mirror BS exactly -- see
`apps/backend/routes/internal-slack-moderators.route.ts` for the source of
truth:

* GET `/` returns 200 with `{items}`, the FULL roster, deliberately
  unpaginated (bounded by the size of the WXYC exec staff and by Slack's
  100-entry `initial_users` cap). Rows carry `slack_user_id`, `added_at`,
  `added_by_slack_user_id`, ordered by `(added_at, slack_user_id)`.
* PUT `/` takes `{slackUserIds, expectedCurrent, actorSlackUserId?}` and
  replaces the whole set, returning 200 with `{items}`. A 409 means the roster
  changed since it was read; its body carries `current`.

Where this deliberately stops mirroring the sibling
--------------------------------------------------
Timeouts. `BanAdminClient` defaults to 10s, which is right for `/admin/bans` --
an operator curling an endpoint will wait. It is wrong here, because every
call this client makes sits inside a Slack deadline:

* The authorization read runs inside the ~3s `trigger_id` window with a
  `views.open` still to follow it, so it gets a **1.0s** budget.
* The roster save runs inside the 3s `view_submission` window -- sharing it
  with the re-authorization read that precedes it -- so it gets **1.5s**.

A 10s default would not merely be slow: it would guarantee `expired_trigger_id`
on the read path and a Slack-rendered timeout on the write path.

Those budgets are *sums of named phases*, not bare floats. httpx applies
connect/read/write/pool independently, so `httpx.Timeout(1.5)` is four
independent 1.5s bounds with a 6s worst case -- a number that silently blows
every window this module reasons about. The constants below name each phase so
the documented budget is the arithmetic rather than an aspiration, and the
tests assert the sum.

One thing the short deadline does NOT solve, and callers must handle: the
damaging case is a Backend-Service that is slow but *succeeding*. It returns a
valid roster, raises nothing, and the fail-closed fallback never fires -- it
has simply eaten the window the caller needed for `views.open`. Timeouts bound
that; they do not make it visible.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

__all__ = [
    "MODERATOR_READ_TIMEOUT",
    "MODERATOR_WRITE_TIMEOUT",
    "ModeratorClient",
    "ModeratorClientError",
    "normalize_slack_user_ids",
]


def normalize_slack_user_ids(user_ids: list[str]) -> list[str]:
    """Uppercase, de-duplicate, and sort a roster for the wire.

    Backend-Service applies exactly this normalization on both write and
    compare, and its route documentation states that ROM normalizes before
    sending -- so doing it here makes that claim true rather than aspirational.

    It also makes the 409 mean what it says. ``expectedCurrent`` is compared as
    a sorted set, so sending an edit that differs from the stored roster only in
    case or ordering would otherwise depend entirely on the far side to avoid a
    spurious conflict on a change that changed nothing.

    Note this is the *wire* contract, and deliberately not the same posture as
    ``resolve_authorized_users``, which unions the environment allowlist
    verbatim. Storage normalizes; authorization comparison does not.

    Raises:
        ModeratorClientError: If any entry is not a non-empty string. The
            client's contract is that every failure funnels into that one
            exception; letting a bare ``AttributeError`` out of the module's
            only public helper would break it for callers that catch only
            ``ModeratorClientError`` -- which, on the authorization path, means
            a 500 instead of a fallback.
    """
    if not all(isinstance(user_id, str) and user_id for user_id in user_ids):
        raise ModeratorClientError(0, {"error": "malformed_user_ids"})
    return sorted({user_id.upper() for user_id in user_ids})


#: Authorization read deadline. Sits inside Slack's ~3s `trigger_id` window
#: *and* leaves room for the `views.open` that follows it. See the module
#: docstring -- this is the number the sibling's 10s default must not become.
#:
#: Phases are named rather than collapsed into one float, for the same reason
#: as ``SLACK_VIEW_OPEN_TIMEOUT``: httpx applies connect/read/write/pool
#: *independently*, so ``httpx.Timeout(1.5)`` is not a 1.5s bound but four of
#: them, worst case 6s. The budget below is the sum, and the sum is what has to
#: fit inside a Slack deadline. ``connect`` carries the largest share because
#: ban clicks are minutes apart and keepalive expires after 5s, so nearly every
#: call pays a fresh handshake.
MODERATOR_READ_TIMEOUT = httpx.Timeout(connect=0.35, read=0.45, write=0.1, pool=0.1)
MODERATOR_READ_BUDGET_SECONDS = 1.0

#: Roster write deadline. Slack shows the submitter an error if
#: `view_submission` doesn't respond within 3s; this leaves headroom to render.
#:
#: Sized against the *whole* handler, not this call alone: the save path
#: re-authorizes first, so a roster read shares the same 3s window. Read budget
#: 1.0 + write budget 1.5 = 2.5s, leaving ~0.5s for delivery, signature
#: verification, and rendering the response.
MODERATOR_WRITE_TIMEOUT = httpx.Timeout(connect=0.4, read=0.8, write=0.2, pool=0.1)
MODERATOR_WRITE_BUDGET_SECONDS = 1.5

#: Largest roster rom will accept from Backend-Service, in rows.
#:
#: This is a *security* bound, not a performance one: every ID in the response
#: is unioned into the set that may ban a listener, so an unbounded read is the
#: only path by which the union can widen without anything failing. WXYC's exec
#: staff is a dozen-ish people; 200 leaves room for a decade of turnover while
#: still refusing a roster that could only be a bug on the far side.
MAX_ROSTER_SIZE = 200


class ModeratorClientError(Exception):
    """Raised when BS rejects a roster call, OR when the rom->BS hop fails at
    the transport layer (DNS, TLS, connect-refused, socket timeout).

    Transport-layer failures -- including a timeout, which is the expected
    failure here rather than an exotic one -- are reported with
    ``status_code=0`` so callers can distinguish "upstream unreachable" from a
    faithful upstream refusal.

    Carries the upstream status code and (best-effort decoded) body. The body
    matters on 409: it holds ``current``, the roster as BS actually has it,
    which is what the modal shows the submitter whose edit was rejected.
    """

    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Backend-Service returned {status_code}: {body!r}")


class ModeratorClient:
    """Thin async client for Backend-Service's Slack ban-moderator roster.

    Args:
        base_url: BS endpoint base URL (no trailing slash required). Example:
            ``https://api.wxyc.org/internal/slack-ban-moderators``.
        http_client: Shared :class:`httpx.AsyncClient` (same singleton used by
            the lookup and ban-admin clients).
        internal_key: Value of ``ROM_INTERNAL_KEY`` on the BS side. Sent as the
            ``X-Internal-Key`` header on every request. Reuses the sibling's
            secret -- BS gates both `/internal` surfaces on the same key.
        read_timeout: Per-call timeout for :meth:`list_moderators`. Defaults to
            :data:`MODERATOR_READ_TIMEOUT`; see the module docstring
            before raising it.
        write_timeout: Per-call timeout for :meth:`replace_moderators`.
            Defaults to :data:`MODERATOR_WRITE_TIMEOUT`.
    """

    def __init__(
        self,
        base_url: str,
        http_client: httpx.AsyncClient,
        *,
        internal_key: str,
        read_timeout: httpx.Timeout | float = MODERATOR_READ_TIMEOUT,
        write_timeout: httpx.Timeout | float = MODERATOR_WRITE_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.http_client = http_client
        self.internal_key = internal_key
        self.read_timeout = read_timeout
        self.write_timeout = write_timeout

    def _headers(self) -> dict[str, str]:
        return {"X-Internal-Key": self.internal_key}

    @staticmethod
    def _decode_body(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return response.text

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        """Decode a 2xx body, raising ModeratorClientError if it isn't JSON.

        BS contract is JSON-on-success, but a reverse proxy or content-type
        drift could return a 200 with HTML. Without this guard the bare
        ``response.json()`` would raise ``ValueError`` and escape the caller's
        ``except ModeratorClientError`` as an unhandled 500 -- on the
        authorization path, that would take the ban button down instead of
        falling back to the environment allowlist.
        """
        try:
            return response.json()
        except ValueError as exc:
            raise ModeratorClientError(
                response.status_code,
                {"error": "non_json_upstream_body", "text": response.text[:200]},
            ) from exc

    @staticmethod
    def _extract_ids(payload: Any) -> list[str]:
        """Pull ``slack_user_id`` out of a BS ``{items}`` envelope, in order.

        Fails closed on any shape that isn't the contract. This list authorizes
        a privileged action, so a response we can't fully parse must raise --
        letting :func:`resolve_authorized_users` shrink to the break-glass
        allowlist -- rather than degrading to a partial roster. A silently
        truncated roster would read as a legitimately smaller one and lock
        people out with no error anywhere.
        """
        if not isinstance(payload, dict):
            raise ModeratorClientError(200, {"error": "malformed_roster_payload"})
        items = payload.get("items")
        if not isinstance(items, list):
            raise ModeratorClientError(200, {"error": "malformed_roster_items"})

        # The grow direction, which the reasoning above omitted. Every ID in
        # this list gains the power to ban a listener, so an oversized roster is
        # the one shape that fails *open*: a Backend-Service regression dropping
        # the WHERE clause would return every user row, and rom would union all
        # of it into the authorized set with nothing raised and nothing logged.
        # Refusing costs a real roster nothing -- MAX_ROSTER_SIZE is two orders
        # of magnitude above WXYC's exec staff -- and turns a silent
        # privilege-escalation into the same break-glass fallback as any other
        # malformed response.
        if len(items) > MAX_ROSTER_SIZE:
            raise ModeratorClientError(
                200, {"error": "roster_too_large", "count": len(items), "max": MAX_ROSTER_SIZE}
            )

        ids: list[str] = []
        for row in items:
            if not isinstance(row, dict):
                raise ModeratorClientError(200, {"error": "malformed_roster_row"})
            user_id = row.get("slack_user_id")
            if not isinstance(user_id, str) or not user_id:
                raise ModeratorClientError(200, {"error": "malformed_roster_user_id"})
            ids.append(user_id)
        return ids

    async def list_moderators(self) -> list[str]:
        """Return the stored roster as Slack user IDs, in BS's order.

        The order is preserved rather than re-sorted because BS's
        ``ORDER BY (added_at, slack_user_id)`` is load-bearing for the modal:
        it is what keeps ``initial_users`` from flapping between renders of an
        unchanged roster.

        An empty list means an empty table -- a legal state, not a failure.

        Raises:
            ModeratorClientError: On any non-2xx, a non-JSON 2xx, a malformed
                success shape, or a transport failure (``status_code=0``),
                including exceeding :data:`MODERATOR_READ_TIMEOUT`.
        """
        try:
            response = await self.http_client.get(
                self.base_url,
                headers=self._headers(),
                timeout=self.read_timeout,
            )
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            raise ModeratorClientError(
                0, {"error": "upstream_unreachable", "detail": str(exc)}
            ) from exc
        if not 200 <= response.status_code < 300:
            raise ModeratorClientError(response.status_code, self._decode_body(response))
        return self._extract_ids(self._safe_json(response))

    async def replace_moderators(
        self,
        *,
        slack_user_ids: list[str],
        expected_current: list[str],
        actor_slack_user_id: str | None = None,
    ) -> list[str]:
        """Replace the whole roster, returning the resulting one.

        Args:
            slack_user_ids: The complete desired roster. An empty list is a
                legal edit meaning "no moderators" and is sent as such.
            expected_current: The roster as it was when the modal opened,
                round-tripped through ``private_metadata``. BS compares against
                this and returns 409 if the stored set moved underneath.
                Normalization (uppercase, dedupe, sort) happens BS-side, so
                this may be sent in whatever order it was read.
            actor_slack_user_id: The moderator who hit Save, recorded on rows
                this call inserts. Omitted from the payload entirely when None.

        Raises:
            ModeratorClientError: On any non-2xx (notably 409, whose body
                carries ``current``), a non-JSON 2xx, a malformed success
                shape, or a transport failure (``status_code=0``).
        """
        payload: dict[str, Any] = {
            "slackUserIds": normalize_slack_user_ids(slack_user_ids),
            "expectedCurrent": normalize_slack_user_ids(expected_current),
        }
        if actor_slack_user_id is not None:
            payload["actorSlackUserId"] = actor_slack_user_id

        try:
            response = await self.http_client.put(
                self.base_url,
                json=payload,
                headers=self._headers(),
                timeout=self.write_timeout,
            )
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            raise ModeratorClientError(
                0, {"error": "upstream_unreachable", "detail": str(exc)}
            ) from exc
        if not 200 <= response.status_code < 300:
            raise ModeratorClientError(response.status_code, self._decode_body(response))
        return self._extract_ids(self._safe_json(response))
