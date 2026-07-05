package com.sense.relay.data

import com.sense.relay.core.model.ApiError
import java.io.IOException
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs

/**
 * Pins [statusApiError]: the pure classifier that turns a thrown fetch
 * error into the small [ApiError] set the UI switches on. The mapping is
 * the only place the "401 → go to Settings" and "network → offline"
 * distinctions are made, so it gets its own focused test.
 */
class StatusApiErrorTest {

    @Test fun securityExceptionMapsToUnauthorized() {
        assertEquals(ApiError.Unauthorized, statusApiError(SecurityException("401")))
    }

    @Test fun ioExceptionMapsToUnreachableWithReason() {
        val err = assertIs<ApiError.Unreachable>(statusApiError(IOException("timeout")))
        assertEquals("timeout", err.reason)
    }

    @Test fun ioExceptionWithoutMessageStillMapsToUnreachable() {
        val err = assertIs<ApiError.Unreachable>(statusApiError(IOException()))
        assertEquals("unreachable", err.reason)
    }

    @Test fun otherThrowableMapsToUnknownCarryingTheCause() {
        val cause = IllegalStateException("boom")
        val err = assertIs<ApiError.Unknown>(statusApiError(cause))
        assertEquals(cause, err.throwable)
    }
}
