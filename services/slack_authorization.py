"""Ban-button authorization allowlist (request-o-matic#152).

``routers/slack_interactivity.py`` calls :func:`is_authorized_slack_user` on
the acting Slack user ID taken from a signature-verified interaction payload
-- never from anything a client sets directly. v1 is a static comma-separated
allowlist (``SLACK_BAN_AUTHORIZED_USERS``); it can graduate to a user-group or
channel-membership check later without changing this call site's contract.
"""

from __future__ import annotations

__all__ = ["is_authorized_slack_user", "parse_authorized_users"]


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


def is_authorized_slack_user(user_id: str | None, allowlist_csv: str | None) -> bool:
    """Return True iff ``user_id`` is on the ``SLACK_BAN_AUTHORIZED_USERS`` allowlist.

    Fails closed: an unset, empty, or all-whitespace/comma allowlist denies
    every user, including one that happens to look like a valid Slack ID. A
    deploy that loses the env var must disable the ban button for everyone,
    not open it to the whole workspace. Comparison is case-sensitive, matching
    Slack's own user ID format.

    Fails closed on shape as well as on value. Both call sites pull ``user_id``
    out of ``json.loads`` output, which is typed ``Any``, so a payload whose
    ``user.id`` is a list or an object would reach the ``in`` test and raise
    ``TypeError: unhashable type`` -- a 500 rather than a refusal. Enforcing the
    declared ``str | None`` contract here closes that at every caller instead of
    relying on each one to remember an isinstance check.
    """
    if not isinstance(user_id, str) or not user_id:
        return False
    return user_id in parse_authorized_users(allowlist_csv)
