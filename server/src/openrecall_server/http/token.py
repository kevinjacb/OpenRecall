"""HTTP/WS shared auth helpers."""
from __future__ import annotations

import hmac


def constant_time_eq(a: str, b: str) -> bool:
    """Compare two strings without leaking length/content via timing.

    Used for bearer-token checks on the WebSocket handshake. Both operands are
    encoded to bytes; ``hmac.compare_digest`` raises ``TypeError`` on mismatched
    types but is constant-time for equal-typed inputs.
    """
    return hmac.compare_digest(a.encode(), b.encode())