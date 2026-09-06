"""Two bearer principals, and which route classes each may reach.

The Android relay and Hermes hold *different* tokens. Hermes reaches /mcp and
nothing else; the relay reaches everything else and never /mcp. A single shared
token would mean a compromised Hermes could drive the whole control API.
"""
from __future__ import annotations

from ..http.token import constant_time_eq

PRINCIPAL_RELAY = "relay"
PRINCIPAL_HERMES = "hermes"

MCP_PREFIX = "/mcp"


def principal_for(request, *, relay_token: str,
                  hermes_token: str | None) -> str | None:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    presented = header[len("Bearer "):]
    # Reuse the existing helper rather than calling hmac.compare_digest on
    # str directly: it encodes to bytes first, and compare_digest raises
    # TypeError on a non-ASCII str.
    if relay_token and constant_time_eq(presented, relay_token):
        return PRINCIPAL_RELAY
    if hermes_token and constant_time_eq(presented, hermes_token):
        return PRINCIPAL_HERMES
    return None


def may_reach(principal: str, path: str) -> bool:
    is_mcp = path == MCP_PREFIX or path.startswith(MCP_PREFIX + "/")
    if principal == PRINCIPAL_HERMES:
        return is_mcp
    return not is_mcp
