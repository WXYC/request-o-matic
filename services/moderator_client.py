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
  `views.open` still to follow it, so it gets **1.5s**.
* The roster save runs inside the 3s `view_submission` window, after which
  Slack shows the submitter a timeout error instead of the result, so it gets
  **2.5s**.

A 10s default would not merely be slow: it would guarantee `expired_trigger_id`
on the read path and a Slack-rendered timeout on the write path. A timeout
raises `ModeratorClientError` like any other transport failure, so the caller's
fail-closed fallback already covers it -- the short deadline is simply what
*triggers* that fallback while it can still do some good.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

__all__ = [
    "MODERATOR_READ_TIMEOUT_SECONDS",
    "MODERATOR_WRITE_TIMEOUT_SECONDS",
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
    """
    return sorted({user_id.upper() for user_id in user_ids})


#: Authorization read deadline. Sits inside Slack's ~3s `trigger_id` window
#: *and* leaves room for the `views.open` that follows it. See the module
#: docstring -- this is the number the sibling's 10s default must not become.
MODERATOR_READ_TIMEOUT_SECONDS = 1.5

#: Roster write deadline. Slack shows the submitter an error if
#: `view_submission` doesn't respond within 3s; this leaves headroom to render.
MODERATOR_WRITE_TIMEOUT_SECONDS = 2.5


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
            :data:`MODERATOR_READ_TIMEOUT_SECONDS`; see the module docstring
            before raising it.
        write_timeout: Per-call timeout for :meth:`replace_moderators`.
            Defaults to :data:`MODERATOR_WRITE_TIMEOUT_SECONDS`.
    """

    def __init__(
        self,
        base_url: str,
        http_client: httpx.AsyncClient,
        *,
        internal_key: str,
        read_timeout: float = MODERATOR_READ_TIMEOUT_SECONDS,
        write_timeout: float = MODERATOR_WRITE_TIMEOUT_SECONDS,
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
                including exceeding :data:`MODERATOR_READ_TIMEOUT_SECONDS`.
        """
        try:
            response = await self.http_client.get(
                self.base_url,
                headers=self._headers(),
                timeout=self.read_timeout,
            )
        except httpx.HTTPError as exc:
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
        except httpx.HTTPError as exc:
            raise ModeratorClientError(
                0, {"error": "upstream_unreachable", "detail": str(exc)}
            ) from exc
        if not 200 <= response.status_code < 300:
            raise ModeratorClientError(response.status_code, self._decode_body(response))
        return self._extract_ids(self._safe_json(response))
