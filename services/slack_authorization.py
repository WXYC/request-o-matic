"""Ban authorization: the environment allowlist unioned with the stored roster.

``routers/slack_interactivity.py`` calls :func:`resolve_authorized_users` on
the acting Slack user ID taken from a signature-verified interaction payload
-- never from anything a client sets directly.

v1 was a static comma-separated allowlist (``SLACK_BAN_AUTHORIZED_USERS``) and
predicted it "can graduate to a user-group or channel-membership check later
without changing this call site's contract" (request-o-matic#152). It graduated
in request-o-matic#240 to the **union** of that allowlist and a roster stored in
Backend-Service (BS#2045). The prediction held in the part that mattered: the
call sites still ask one question and get one boolean, they just ``await`` it
now.

``SLACK_BAN_AUTHORIZED_USERS`` survives as a small break-glass superuser list,
not as the roster. That is what makes the fail-closed direction below coherent:
losing Backend-Service costs you the roster and leaves the break-glass list,
which is the correct thing to be left holding.
"""

from __future__ import annotations

import logging

from services.moderator_client import ModeratorClient, ModeratorClientError

logger = logging.getLogger(__name__)

__all__ = [
    "is_authorized_slack_user",
    "is_authorized_slack_user_in",
    "parse_authorized_users",
    "resolve_authorized_users",
]


def parse_authorized_users(allowlist_csv: str | None) -> frozenset[str]:
    """Parse ``SLACK_BAN_AUTHORIZED_USERS`` into a set of Slack user IDs.

    Whitespace around entries is trimmed; empty entries (a stray comma, a
    trailing comma, an unset/blank variable) are dropped rather than
    producing a set containing ``""`` -- an empty string must never compare
    equal to a missing user ID.
    """
    if not allowlist_csv:
        return frozenset()
    return frozenset(entry.strip() for entry in allowlist_csv.split(",") if entry.strip())


def is_authorized_slack_user_in(user_id: str | None, authorized: frozenset[str]) -> bool:
    """Return True iff ``user_id`` is in the already-resolved ``authorized`` set.

    Fails closed on shape as well as on value. Call sites pull ``user_id`` out
    of ``json.loads`` output, which is typed ``Any``, so a payload whose
    ``user.id`` is a list or an object would reach the ``in`` test and raise
    ``TypeError: unhashable type`` -- a 500 rather than a refusal, and on this
    route an uncaught exception is a 500 whose Sentry event carries the
    settings object. Enforcing the declared ``str | None`` contract here closes
    that at every caller instead of relying on each one to remember an
    isinstance check.

    Comparison is case-sensitive, matching Slack's own user ID format. See
    :func:`resolve_authorized_users` for why the two halves of the union
    deliberately differ on case normalization.
    """
    if not isinstance(user_id, str) or not user_id:
        return False
    return user_id in authorized


def is_authorized_slack_user(user_id: str | None, allowlist_csv: str | None) -> bool:
    """Return True iff ``user_id`` is on the ``SLACK_BAN_AUTHORIZED_USERS`` allowlist.

    Fails closed: an unset, empty, or all-whitespace/comma allowlist denies
    every user, including one that happens to look like a valid Slack ID. A
    deploy that loses the env var must disable the ban button for everyone,
    not open it to the whole workspace.

    This is the environment half alone. Callers that need the full authorized
    set want :func:`resolve_authorized_users`; this remains for the break-glass
    path and for callers with no Backend-Service client to hand.
    """
    return is_authorized_slack_user_in(user_id, parse_authorized_users(allowlist_csv))


async def resolve_authorized_users(
    client: ModeratorClient | None,
    allowlist_csv: str | None,
) -> frozenset[str]:
    """Return the union of the environment allowlist and the stored roster.

    Args:
        client: The Backend-Service roster client, or None when the upstream
            is unconfigured. None is treated exactly like an unreachable
            upstream -- see below.
        allowlist_csv: ``SLACK_BAN_AUTHORIZED_USERS``, the break-glass half.

    **Fails closed.** On a :class:`ModeratorClientError` -- which covers a
    timeout, a transport failure, an upstream refusal, and a malformed
    response -- or on a None client, this returns the environment allowlist
    *alone*. An unreachable Backend-Service must shrink the authorized set to
    the break-glass list, never widen it: reading an upstream error as "allow"
    would turn a BS outage into workspace-wide ban rights.

    A None client is not an error worth raising. ``get_moderator_client``
    returns None when ``BS_INTERNAL_MODERATORS_URL`` or ``BS_INTERNAL_KEY`` is
    unset, rather than raising 503 the way ``get_ban_admin_client`` does. A 503
    is right for ``/admin/bans``, where the whole request *is* the upstream
    call; it is wrong here, where a 503 out of the authorization path would
    take the ban button down along with the roster it was only trying to read.

    **The two halves are deliberately asymmetric on case, and that is not a
    bug to fix.** Backend-Service normalizes the stored roster to uppercase
    because its ``expectedCurrent`` comparison depends on it; that
    normalization is storage-side only. The environment half is unioned
    verbatim, preserving the documented case-sensitive comparison in
    :func:`is_authorized_slack_user_in`. Uppercasing the environment side too
    would silently change a shipped, documented security contract for no
    benefit: Slack user IDs arrive uppercase from Slack, so the only IDs a fold
    could rescue are hand-typed lowercase entries in the env var, which are
    already broken today and should be fixed in the variable rather than
    papered over in the predicate.
    """
    environment = parse_authorized_users(allowlist_csv)

    if client is None:
        logger.debug(
            "Moderator roster upstream unconfigured; authorizing off the "
            "environment allowlist alone (%d entries)",
            len(environment),
        )
        return environment

    try:
        stored = await client.list_moderators()
    except ModeratorClientError as exc:
        logger.warning(
            "Moderator roster unavailable (%s); falling back to the environment "
            "allowlist alone (%d entries)",
            exc,
            len(environment),
        )
        return environment

    return environment | frozenset(stored)
