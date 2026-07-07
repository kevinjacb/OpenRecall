package com.sense.relay.net

import java.net.URI

/**
 * Derive the WebSocket gateway URL from the provisioned HTTP server URL and the
 * gateway port the server reports.
 *
 * The server runs two listeners on separate ports: an HTTP control API (the
 * URL the phone provisions against — `/status`, `/sessions`, …) and the WS
 * gateway (real-time §E control + §C.6 audio). The phone can't reach the WS
 * gateway by swapping only the scheme on the HTTP URL — that would keep the
 * HTTP port, hit the HTTP server (which has no WS route), and fail the
 * `101 Switching Protocols` upgrade with "Expected HTTP 101 response".
 *
 * So the server reports its WS gateway port via `/health` (see
 * [com.sense.relay.http.SenseHttpClient.gatewayPort]); provisioning stores it
 * in [com.sense.relay.store.Config.gatewayPort]; the relay passes it here.
 *
 * - scheme: `wss` if the HTTP URL is `https`, else `ws`.
 * - host: parsed from [serverUrl] (the host the phone already reaches for HTTP).
 * - port: [gatewayPort] when non-null (the fix); otherwise the port in
 *   [serverUrl] (back-compat with a server that doesn't report a port — same
 *   host:port as HTTP, the old scheme-swap behavior); otherwise omitted
 *   (default port).
 * - path/query are dropped — the WS gateway (`websockets.serve`) accepts any
 *   path; the HTTP API's path is irrelevant to it.
 *
 * Returns [serverUrl] with only the scheme swapped if the host can't be parsed
 * (defensive: never return a malformed URL for a relay connection).
 */
fun wsGatewayUrl(serverUrl: String, gatewayPort: Int?): String {
    val scheme = if (serverUrl.startsWith("https")) "wss" else "ws"
    val uri = runCatching { URI(serverUrl) }.getOrNull()
    val host = uri?.host
    if (host.isNullOrBlank()) {
        // Can't parse a host — fall back to the legacy scheme-swap so we never
        // hand the socket a malformed URL. (In practice the URL is always
        // http(s)://host:port, so this branch is unreachable.)
        return serverUrl.replaceFirst(Regex("^https?://"), "$scheme://")
    }
    val port = gatewayPort ?: uri.port.takeIf { it != -1 }
    return if (port != null) "$scheme://$host:$port" else "$scheme://$host"
}