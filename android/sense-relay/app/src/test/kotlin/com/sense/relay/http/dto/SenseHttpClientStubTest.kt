package com.sense.relay.http.dto

import com.sense.relay.domain.model.SessionId
import com.sense.relay.http.SenseHttpClient
import kotlinx.coroutines.test.runTest
import java.io.IOException
import kotlin.test.Test
import kotlin.test.assertFailsWith

/**
 * Phase 2 stubs: every server-touching method on [SenseHttpClient]
 * throws a stable `IOException("not yet wired (Phase 3 server
 * endpoint)")`. Phase 3 lands the real OkHttp calls; the existing
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

    @Test fun getStatusThrowsIoException() = runTest {
        val ex = assertFailsWith<IOException> { client().getStatus() }
        assertMessage(ex, "getStatus")
    }

    private fun assertMessage(ex: IOException, method: String) {
        // The stub message is allowed to drift slightly (Phase 3 may
        // add the URL), but it MUST mention the method and the phase
        // so logcat greppers can find it.
        val msg = ex.message ?: ""
        check(method in msg) { "expected method '$method' in '$msg'" }
        check("Phase 3" in msg) { "expected 'Phase 3' in '$msg'" }
    }
}
