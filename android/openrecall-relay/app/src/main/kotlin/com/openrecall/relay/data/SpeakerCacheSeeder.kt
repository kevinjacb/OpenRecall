package com.openrecall.relay.data

import com.openrecall.relay.core.RecallLog
import com.openrecall.relay.relay.ServerState
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.filter
import kotlinx.coroutines.launch

/**
 * Keeps the shared [SpeakerCache] in sync with the server's `GET /speakers`.
 *
 * The cache drives the Recordings Reassign picker and the cache-fallback label
 * resolution. It is also upserted by every live transcript §E, but §E only
 * arrives while the relay is actively streaming — a session opened from the
 * Recordings tab with no live stream would otherwise see an empty picker even
 * when the server knows the speakers. This seeder populates the cache from the
 * server on every (re)connect so the picker is ready before the user opens it.
 *
 * **Trigger:** the transition into [ServerState.Authenticated] (published by
 * `RelayService` on socket open). Fires on first connect, every reconnect, and
 * after a re-provision — exactly the moments the server is confirmed up and the
 * bearer token was just accepted. [SpeakerCache.seed] replaces the set, so
 * idempotent re-seeding is safe. A fetch failure is logged, not thrown, so the
 * collector survives to retry on the next authenticated tick.
 *
 * Testability: [seedOnce] is a thin suspend function (throws on API failure;
 * the caller catches); [launchOnAuthenticated] takes the upstream `Flow` so a
 * test can drive a `MutableStateFlow` instead of the real `RelayController`.
 */
class SpeakerCacheSeeder(
    private val repo: SpeakerRepository,
    private val cache: SpeakerCache,
) {
    /** Fetch `/speakers` and replace the cache. Throws if the API fails;
     *  [launchOnAuthenticated] catches. */
    suspend fun seedOnce() {
        cache.seed(repo.loadSpeakers())
    }

    /** Re-seed on every transition into [ServerState.Authenticated]. Errors are
     *  logged, not thrown, so a bad fetch never kills the collector. */
    fun launchOnAuthenticated(scope: CoroutineScope, serverStates: Flow<ServerState>) {
        scope.launch {
            serverStates
                .distinctUntilChanged()
                .filter { it is ServerState.Authenticated }
                .collect {
                    runCatching { seedOnce() }
                        .onFailure { e ->
                            RecallLog.e(
                                tag = TAG,
                                msg = "speaker cache seed failed: ${e.javaClass.simpleName}",
                                t = e,
                            )
                        }
                }
        }
    }

    private companion object {
        const val TAG = "SpeakerCacheSeeder"
    }
}