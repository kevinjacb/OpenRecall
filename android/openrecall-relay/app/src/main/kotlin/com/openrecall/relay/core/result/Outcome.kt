package com.openrecall.relay.core.result

import com.openrecall.relay.core.model.ApiError

/**
 * Cross-cutting result envelope. Repositories return `Flow<Outcome<T>>`;
 * ViewModels `map`; the UI switches on the sealed type. `Failure` is a
 * sealed-class-friendly way to avoid throwing across coroutine boundaries.
 */
sealed interface Outcome<out T> {
    data class Success<T>(val value: T) : Outcome<T>
    data class Failure(val error: ApiError) : Outcome<Nothing>
}

/** Map a successful value; pass a failure through untouched. */
inline fun <T, R> Outcome<T>.map(f: (T) -> R): Outcome<R> = when (this) {
    is Outcome.Success -> Outcome.Success(f(value))
    is Outcome.Failure -> this
}

/** Get the success value, or null on failure. */
fun <T> Outcome<T>.getOrNull(): T? = when (this) {
    is Outcome.Success -> value
    is Outcome.Failure -> null
}
