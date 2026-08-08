package com.opensapien.relay.data

import com.opensapien.relay.core.model.ApiError
import java.io.IOException
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs

/**
 * Pins [httpApiError]: the pure classifier that turns a thrown HTTP error
 * into the small [ApiError] set the UI switches on. Shared by the status
 * poller and the session repository — it is the only place the "401 → go to
 * Settings" and "network → offline" distinctions are made.
 */
class HttpApiErrorTest {

    @Test fun securityExceptionMapsToUnauthorized() {
        assertEquals(ApiError.Unauthorized, httpApiError(SecurityException("401")))
    }

    @Test fun ioExceptionMapsToUnreachableWithReason() {
        val err = assertIs<ApiError.Unreachable>(httpApiError(IOException("timeout")))
        assertEquals("timeout", err.reason)
    }

    @Test fun ioExceptionWithoutMessageStillMapsToUnreachable() {
        val err = assertIs<ApiError.Unreachable>(httpApiError(IOException()))
        assertEquals("unreachable", err.reason)
    }

    @Test fun otherThrowableMapsToUnknownCarryingTheCause() {
        val cause = IllegalStateException("boom")
        val err = assertIs<ApiError.Unknown>(httpApiError(cause))
        assertEquals(cause, err.throwable)
    }

    @Test fun httpStatusExceptionMapsToApiErrorHttpWithCode() {
        // A non-auth non-2xx response surfaces its code so the UI can show
        // "Server is starting up" for a 503, not the generic "Can't reach
        // the server". Matched before the IOException branch (HttpStatusException
        // is an IOException subclass).
        val err = assertIs<ApiError.Http>(httpApiError(com.opensapien.relay.http.HttpStatusException(503)))
        assertEquals(503, err.code)
    }
}
