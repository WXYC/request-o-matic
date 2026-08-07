"""Shared validation for the ``X-Device-Fingerprint`` request header.

Every consumer of the raw header value runs through :func:`normalize_fingerprint`
so that "malformed" and "absent" are indistinguishable downstream. Two callers
depend on that today, for different reasons:

* ``services/ban_check_client.py`` — a malformed fingerprint makes BS reply 400,
  which ROM surfaces as ``BanCheckUnavailableError`` and the router fails open.
  A banned listener could bypass enforcement just by appending garbage to the
  header (WXYC/request-o-matic#150).
* ``services/slack.py`` — a fingerprint that isn't a UUID can't be banned.
  ``POST /admin/bans`` types the field as ``UUID`` and hard-rejects anything
  else with 422, while WXYC/request-o-matic#152 renders its "Ban requester"
  button off metadata *presence*. Attaching an unusable value would leave a
  permanently broken button on exactly the abusive listener the feature exists
  to ban. Bounding the value to a UUID additionally keeps a caller-controlled
  string out of ``chat.postMessage``, whose ``metadata_too_large`` /
  ``invalid_metadata_format`` errors come back as HTTP 200 ``{"ok": false}`` and
  cost the listener their whole request via a 502.

Keeping one regex here (rather than a copy per consumer) means those two
policies can never drift apart -- a value ROM is willing to ban on is exactly
the value ROM is willing to advertise as bannable.
"""

from __future__ import annotations

import re

__all__ = ["normalize_fingerprint"]

# Permissive UUID regex matching BS's UUID_REGEX in
# apps/auth/check-request-ban-handler.ts — accepts any UUID version, either
# case. Deliberately not `uuid.UUID()`: that accepts braced, urn-prefixed, and
# hyphen-less spellings BS would reject, so parsing successfully here would
# still hand BS a value it 400s on.
_FINGERPRINT_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def normalize_fingerprint(fingerprint: str | None) -> str | None:
    """Return ``fingerprint`` iff it is a well-formed UUID, else None.

    Note that FastAPI binds a *present but empty* header to ``""``, not None, so
    callers must not substitute an ``is None`` check for this one.

    Args:
        fingerprint: Raw value of the ``X-Device-Fingerprint`` header, or None.

    Returns:
        The fingerprint with surrounding whitespace stripped, preserving case
        (BS's regex is case-insensitive and the echoed value must still identify
        the device that sent it). None when the value is absent, empty,
        whitespace-only, or not a UUID.
    """
    if fingerprint is None:
        return None
    candidate = fingerprint.strip()
    if not _FINGERPRINT_RE.match(candidate):
        return None
    return candidate
