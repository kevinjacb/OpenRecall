package com.sense.relay.http.dto

import com.sense.relay.domain.model.SessionId
import com.sense.relay.http.SenseHttpClient
import kotlinx.coroutines.test.runTest
import java.io.IOException
import kotlin.test.Test
import kotlin.test.assertFailsWith

/**
 * Session-listing stubs on [SenseHttpClient] throw a stable
 * `IOException` naming the method + the phase that wires it (Phase 5).
 * `getStatus()` is no longer a stub — Phase 4 wired the real
 * `GET /status` call, so it's exercised through
 * [com.sense.relay.data.PollingStatusRepository] instead of here.
 * `health()` and `serverPubkey()` keep working unchanged.
 *
 * The test instantiates the client directly. Its constructor builds
 * an `OkHttpClient` eagerly but does no I/O on the host JVM (no
 * `caPem` → no SSL context init), so the stubs are reachable from
 * a host unit test.
 */
class SenseHttpClientStubTest {

    private fun client() = SenseHttpClient(baseUrl = "https://x:8766", token = "tok")

    @Test fun listSessionsThrowsIoException() = runTest {
        val ex = assertFailsWith<IOException> { client().listSessions() }
        assertMessage(ex, "listSessions")
    }

    @Test fun listSessionsWithCursorAndLimitThrowsIoException() = runTest {
        val ex = assertFailsWith<IOException> { client().listSessions(limit = 5, cursor = "cur") }
        assertMessage(ex, "listSessions")
    }

    @Test fun getSessionThrowsIoException() = runTest {
        val ex = assertFailsWith<IOException> { client().getSession(SessionId("s1")) }
        assertMessage(ex, "getSession")
    }

    @Test fun getSessionEventsThrowsIoException() = runTest {
        val ex = assertFailsWith<IOException> { client().getSessionEvents(SessionId("s1")) }
        assertMessage(ex, "getSessionEvents")
    }

    private fun assertMessage(ex: IOException, method: String) {
        // The stub message is allowed to drift slightly (a later phase may
        // add the URL), but it MUST mention the method and the phase that
        // wires it so logcat greppers can find it.
        val msg = ex.message ?: ""
        check(method in msg) { "expected method '$method' in '$msg'" }
        check("Phase 5" in msg) { "expected 'Phase 5' in '$msg'" }
    }
}
