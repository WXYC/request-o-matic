"""User-Agent gate for request-line ban enforcement (WXYC/request-o-matic#155).

The BS ban check (#150) blocks requests when BS confirms the caller is banned.
That defends against every evasion path except "strip both Authorization and
X-Device-Fingerprint headers entirely" — for which ROM proceeds-as-unauth (a
v3.1 backward-compatibility constraint).

This module closes that gap for *known* clients. iOS 3.2+ unconditionally sends
``User-Agent: WXYC-iOS/<version>`` plus ``X-Device-Fingerprint``. If the UA
identifies the caller as a recent WXYC client AND the fingerprint header is
missing, the request is rejected (403) before reaching the BS round-trip.

Unknown UAs (curl, browsers, v3.1 iOS, anything not registered below) keep the
existing lenient behavior so legacy traffic isn't broken.

Spoofing the UA is in scope but not a blocker: an attacker who claims to be
``WXYC-iOS/3.2.0`` has to then also send a fingerprint, at which point they're
back on the fingerprint-banned path the BS ban check (#150) enforces.
"""

from __future__ import annotations

import logging
import re

__all__ = ["UA_GATE_BAN_REASON", "UA_GATE_BAN_SOURCE", "is_known_strict_client"]

logger = logging.getLogger(__name__)

# Constants for the PostHog ``request_blocked`` event emitted when this gate
# rejects. Kept here next to the matcher so a future Android entry can introduce
# its own reason without scattering literals across the router.
UA_GATE_BAN_SOURCE = "rom_strict_mode"
UA_GATE_BAN_REASON = "ua_gate_missing_fingerprint"

# Map from product token (the part before the slash in a User-Agent product
# clause) to the minimum version tuple at-or-above which the client is expected
# to send ``X-Device-Fingerprint`` unconditionally. Adding ``"WXYC-Android"``
# with its own threshold here is the only change needed when the Android team
# ships ban-aware code.
_STRICT_CLIENT_REGISTRY: dict[str, tuple[int, ...]] = {
    "WXYC-iOS": (3, 2),
}

# Matches "<product>/<version>" tokens. Captures the dotted-numeric version
# directly after the slash and ignores any ``-beta`` or ``+metadata`` suffix.
# Apple-style URLSession user agents append " CFNetwork/... Darwin/..." which
# this regex picks up as separate tokens; non-registered products are skipped.
_PRODUCT_TOKEN_RE = re.compile(r"([A-Za-z0-9\-]+)/(\d+(?:\.\d+)*)")


def is_known_strict_client(user_agent: str | None) -> bool:
    """Return True iff ``user_agent`` claims a registered client at-or-above its strict-mode version.

    Returning True means "we know this client *should* be sending the
    fingerprint; if it isn't, treat that as evasion." Returning False means
    "we have no opinion about this caller; proceed under the existing lenient
    contract."

    Args:
        user_agent: Raw value of the ``User-Agent`` request header, or None.

    Returns:
        True when the UA contains a registered product token whose version is
        ``>=`` the registry minimum. False otherwise (unknown clients,
        malformed UAs, or no UA at all).
    """
    if not user_agent:
        return False

    for product, version in _PRODUCT_TOKEN_RE.findall(user_agent):
        minimum = _STRICT_CLIENT_REGISTRY.get(product)
        if minimum is None:
            continue
        try:
            parsed = tuple(int(part) for part in version.split("."))
        except ValueError:
            # Defense-in-depth: the regex already restricts to dotted-numeric,
            # so int() should never fail. Keep the guard so a future regex
            # loosening doesn't silently turn malformed UAs into True.
            continue
        if parsed >= minimum:
            return True

    return False
