package com.opensapien.relay.core.util

import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.mapLatest

/**
 * `Flow<T>` utilities used by every ViewModel. These are intentionally tiny
 * so the behavior is easy to verify in tests; we add complexity only when
 * a real consumer needs it.
 */

/**
 * Emit [initial] before any upstream value, then emit every upstream value
 * verbatim. Useful for "always show the current cached value, even before
 * the first refresh completes" patterns.
 */
fun <T> Flow<T>.startWith(initial: T): Flow<T> = flow {
    emit(initial)
    collect { emit(it) }
}

/**
 * `mapLatest` that swallows the cancellation of in-flight transforms when
 * a new upstream value arrives. Equivalent to `mapLatest` for most uses
 * — the "safe" suffix is a marker for the rare caller that wants a
 * different policy (e.g. a later phase may add try/catch here).
 */
@OptIn(kotlinx.coroutines.ExperimentalCoroutinesApi::class)
fun <T, R> Flow<T>.mapLatestSafe(transform: suspend (T) -> R): Flow<R> =
    mapLatest(transform)
