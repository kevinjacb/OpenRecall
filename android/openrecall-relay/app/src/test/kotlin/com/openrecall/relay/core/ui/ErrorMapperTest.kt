package com.openrecall.relay.core.ui

import com.openrecall.relay.core.model.ApiError
import kotlin.test.Test
import kotlin.test.assertEquals

/**
 * Pins [toDisplayMessage]: each [ApiError] variant maps to a stable,
 * user-facing string. No raw exception text reaches the UI.
 */
class ErrorMapperTest {

    @Test fun unauthorizedTellsTheUserToSignInAgain() {
        assertEquals("Token rejected — sign in again", ApiError.Unauthorized.toDisplayMessage())
    }

    @Test fun unreachableIsGenericConnectionFailure() {
        assertEquals("Can't reach the server", ApiError.Unreachable("timeout").toDisplayMessage())
        assertEquals("Can't reach the server", ApiError.Unreachable("DNS").toDisplayMessage())
    }

    @Test fun http503IsServerStartingUp() {
        assertEquals("Server is starting up", ApiError.Http(503).toDisplayMessage())
    }

    @Test fun http5xxIsGenericServerError() {
        assertEquals("Server error (500)", ApiError.Http(500).toDisplayMessage())
        assertEquals("Server error (502)", ApiError.Http(502).toDisplayMessage())
    }

    @Test fun http4xxIsCouldntReach() {
        assertEquals("Couldn't reach the server (404)", ApiError.Http(404).toDisplayMessage())
        assertEquals("Couldn't reach the server (429)", ApiError.Http(429).toDisplayMessage())
    }

    @Test fun unknownIsGeneric() {
        assertEquals("Something went wrong", ApiError.Unknown(IllegalStateException("boom")).toDisplayMessage())
    }
}