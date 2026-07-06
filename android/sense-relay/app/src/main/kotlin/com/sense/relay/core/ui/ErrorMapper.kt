package com.sense.relay.core.ui

import com.sense.relay.core.model.ApiError

/**
 * The single source of user-facing error strings. Maps each [ApiError]
 * variant to a stable, human-readable message — no raw exception text
 * (`throwable.message`) reaches the UI. Pure (no I/O, no Android imports),
 * so it's host-testable in isolation.
 *
 * The messages are deliberately generic + actionable: an `Unauthorized`
 * tells the user to re-sign-in (the Settings/re-provision path); an
 * `Unreachable` is the "check your connection" case; a 503 is surfaced as
 * "starting up" (the server's own boot window); other 5xx is a generic
 * server error; 4xx is "couldn't reach" (the server replied but the request
 * was bad — rare for a bearer-token GET).
 */
fun ApiError.toDisplayMessage(): String = when (this) {
    ApiError.Unauthorized -> "Token rejected — sign in again"
    is ApiError.Unreachable -> "Can't reach the server"
    is ApiError.Http -> when (code) {
        503 -> "Server is starting up"
        in 500..599 -> "Server error ($code)"
        in 400..499 -> "Couldn't reach the server ($code)"
        else -> "Couldn't reach the server ($code)"
    }
    is ApiError.Unknown -> "Something went wrong"
}