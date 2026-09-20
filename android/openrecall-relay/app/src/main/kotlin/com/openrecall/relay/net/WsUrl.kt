package com.openrecall.relay.net

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
 * [com.openrecall.relay.http.OpenRecallHttpClient.gatewayPort]); provisioning stores it
 * in [com.openrecall.relay.store.Config.gatewayPort]; the relay passes it here.
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
/**
 * Which gateway port the relay should use on a (re)start.
 *
 * Extracted from `RelayService.onStartCommand` so the rule is testable — the
 * service itself is Android wiring with no unit-test harness, and this rule
 * has a sharp edge.
 *
 * `extra` (an intent extra from provisioning) wins when present. Otherwise the
 * persisted value is adopted **including null**, which is where this differs
 * from the `serverUrl` and `token` fallbacks beside it. Those treat empty as
 * "not configured" and keep the existing value, because there is a usable
 * default to protect. A gateway port has no default: null is a real setting
 * meaning "reuse the port in the HTTP URL" — what a tunnel on 443 needs.
 *
 * Guarding this with `?.let` (as the service did) made clearing the override
 * impossible: the old port survived in memory across a restart, so Settings
 * could raise the port but never lower it back to the URL's.
 */
fun resolveGatewayPort(extra: Int?, persisted: Int?): Int? = extra ?: persisted
