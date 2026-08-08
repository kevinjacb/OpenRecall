package com.openrecall.relay.core.util

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * Cadence for the read screens' auto-refresh (Recordings, session detail,
 * Memories). One second: the product wants those screens to track the server
 * as it transcribes, so a session that is still growing visibly grows.
 *
 * The loop is foreground-only — the routes start it on `ON_RESUME` and stop
 * it on `ON_PAUSE` — and every tick is *silent*: it never flips the screen
 * back to a spinner and never replaces loaded content with an error. A tick
 * that fails simply leaves the last good data on screen until the next one.
 */
const val AUTO_REFRESH_INTERVAL_MS: Long = 1_000L

/**
 * Launch a poll loop that runs [tick] every [intervalMs] until the returned
 * [Job] is cancelled. The first tick fires *after* one interval — every
 * caller already loads on entry, so an immediate tick would duplicate that
 * fetch.
 *
 * A non-cancellation throwable from [tick] is swallowed so one bad cycle
 * can't kill the loop (and, under a supervisor scope, can't reach the
 * process's default handler). [delayFn] is a test seam: injecting a
 * recording/short-circuiting delay lets a unit test assert the cadence
 * without wall-clock waiting.
 */
fun CoroutineScope.launchAutoRefresh(
    intervalMs: Long = AUTO_REFRESH_INTERVAL_MS,
    delayFn: suspend (Long) -> Unit = { delay(it) },
    tick: suspend () -> Unit,
): Job = launch {
    while (isActive) {
        delayFn(intervalMs)
        try {
            tick()
        } catch (e: CancellationException) {
            throw e
        } catch (_: Throwable) {
            // Keep the loop alive; the next tick tries again.
        }
    }
}
