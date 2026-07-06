package com.sense.relay.data

import com.sense.relay.core.model.ApiError
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
}
