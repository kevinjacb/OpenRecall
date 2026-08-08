package com.openrecall.relay.domain.model

/**
 * The server-side status snapshot rendered on the Dashboard. All
 * fields are present regardless of reachability; `reachable = false`
 * means the rest are last-known (or zeros if never reached). Auth
 * state is the bearer-token status — a 401 is the "go to Settings"
 * signal.
 *
 * Produced by `GET /status` (Phase 3) and pulled by
 * `PollingStatusRepository` (Phase 4).
 */
data class ServerStatus(
    val reachable: Boolean,
    val authenticated: Boolean,
    val version: String?,
    val uptimeSeconds: Long,
    val activeSessions: Int,
    val totalSessions: Int,
    val recentEvents24h: Int,
)
