package com.openrecall.relay.data

import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import com.openrecall.relay.core.model.ApiError
import com.openrecall.relay.core.result.Outcome
import com.openrecall.relay.domain.model.ServerStatus
import com.openrecall.relay.http.HttpStatusException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.async
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.filterNotNull
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.io.IOException

/**
 * Foreground-only poller for `GET /status`. The real [StatusRepository]:
 * on every `ON_START` of the observed [Lifecycle] it launches a loop that
 * fetches the status, caches the [Outcome], waits [intervalMs], and
 * repeats; on `ON_STOP` it cancels the loop. Production observes
 * `ProcessLifecycleOwner.get().lifecycle`, so polling runs only while the
 * app is in the foreground and stops automatically when it's backgrounded.
 *
 * **Test seams (all constructor params have production defaults):**
 *  - [fetch]: the one thing that touches the network. Tests inject a fake
 *    that returns a controllable [Outcome] without any I/O. Production wires
 *    it to a `OpenRecallHttpClient.getStatus()` call (see [RepositoryModule]).
 *  - [intervalMs] / [delayFn]: the cadence. Tests inject a [delayFn] that
 *    records the requested durations (and/or short-circuits the wait) so a
 *    unit test can assert the 2s cadence without wall-clock waiting.
 *  - [scope]: the coroutine scope the loop runs in. Tests pass a
 *    test scope so the loop advances on virtual time.
 *
 * The cached value is exposed via [observeStatus] as a `filterNotNull`
 * over the internal [MutableStateFlow], so a subscriber gets the latest
 * poll result immediately on subscribe and the loop is independent of
 * whether anyone is subscribed.
 */
class PollingStatusRepository(
    private val fetch: suspend () -> Outcome<ServerStatus>,
    lifecycle: Lifecycle,
    private val intervalMs: Long = DEFAULT_INTERVAL_MS,
    private val delayFn: suspend (Long) -> Unit = { delay(it) },
    private val scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.Default),
) : StatusRepository {

    private val cached = MutableStateFlow<Outcome<ServerStatus>?>(null)
    private var job: Job? = null

    init {
        lifecycle.addObserver(object : DefaultLifecycleObserver {
            override fun onStart(owner: LifecycleOwner) = start()
            override fun onStop(owner: LifecycleOwner) = stop()
        })
    }

    /** Test-only: the latest cached poll result (null before the first
     *  poll completes). Reading this avoids the `StateFlow.collect` /
     *  `TestDispatcher` friction in unit tests. */
    internal val latest: Outcome<ServerStatus>?
        get() = cached.value

    private fun start() {
        if (job?.isActive == true) return
        job = scope.launch {
            while (isActive) {
                // Defense in depth: `fetch` is contracted to return an
                // Outcome (never throw), and the production fetch honors
                // that. But a thrown exception here would escape the
                // `launch` with no CoroutineExceptionHandler and — under a
                // SupervisorJob — route to the process's default handler
                // (an app crash), not merely stall this loop. So we swallow
                // any non-cancellation throwable and keep polling; the
                // cached value simply stays as-is for that cycle.
                try {
                    cached.value = fetch()
                } catch (e: CancellationException) {
                    throw e
                } catch (_: Throwable) {
                    // Keep the loop alive; the next tick tries again.
                }
                delayFn(intervalMs)
            }
        }
    }

    private fun stop() {
        job?.cancel()
        job = null
    }

    /**
     * Force one fetch right now, on the poller's scope/dispatcher, and publish
     * the result. Used by pull-to-refresh so a gesture re-fetches `/status`
     * without waiting for the next 2s tick. Runs on [scope] (so the blocking
     * OkHttp call stays off the caller's dispatcher) and awaits completion so
     * the caller can toggle a refresh spinner around it. A thrown fetch is
     * swallowed exactly like the loop does — the cache simply stays as-is.
     */
    override suspend fun refresh() {
        scope.async {
            try {
                cached.value = fetch()
            } catch (e: CancellationException) {
                throw e
            } catch (_: Throwable) {
                // Mirror the loop: keep the cache as-is on a thrown fetch.
            }
        }.await()
    }

    override fun observeStatus(): Flow<Outcome<ServerStatus>> = cached.filterNotNull()

    companion object {
        /** The spec's status cadence: poll every 2 seconds while foregrounded. */
        const val DEFAULT_INTERVAL_MS: Long = 2_000L
    }
}

/**
 * Classify a thrown HTTP error into the small [ApiError] set the UI cares
 * about. Pure (no I/O, no Android) so it is unit-testable in isolation, and
 * shared across the repositories that call [OpenRecallHttpClient] (the status
 * poller and the session repository):
 *  - [SecurityException] → [ApiError.Unauthorized] (the client throws this
 *    on a 401/403).
 *  - [HttpStatusException] → [ApiError.Http]`code` (a non-auth non-2xx
 *    response — e.g. 503, which the UI renders as "Server is starting up").
 *    Matched BEFORE [IOException] because [HttpStatusException] is an
 *    [IOException] subclass.
 *  - [IOException] → [ApiError.Unreachable] (DNS/TCP/TLS/timeout — the
 *    client never reaches the server, or the connection drops).
 *  - anything else → [ApiError.Unknown] (carries the throwable for logging).
 */
fun httpApiError(e: Throwable): ApiError = when (e) {
    is SecurityException -> ApiError.Unauthorized
    is HttpStatusException -> ApiError.Http(e.code)
    is IOException -> ApiError.Unreachable(e.message ?: "unreachable")
    else -> ApiError.Unknown(e)
}
