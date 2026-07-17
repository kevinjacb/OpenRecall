package com.sense.relay.arch

import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Architectural invariant: the `ui/` package must not import
 * anything from `com.sense.relay.http` or `com.sense.relay.http.dto`.
 *
 * This is the matching guard for INV-11 on the UI side. The
 * repository (data layer) is the only place that knows about
 * `HttpApiError` and the wire DTOs; the UI consumes only domain
 * types from `data/`. If a future change adds a `http.*` import
 * to a UI file (other than the [preExistingAllowList] below), this
 * test fails.
 *
 * Pure file-tree scan — no Android, no reflection, runs in ms.
 */
class ArchitecturalInvariantsTest {

    /**
     * Pre-existing violations that pre-date the
     * CommandsScreen / CommandsRoute / etc. work in this PR. They
     * are tracked as tech debt and should be fixed in a dedicated
     * follow-up (extract a `ServerApi` interface; have
     * `SetupActivity` consume a `RepositoryModule.repos.*` accessor
     * instead of `SenseHttpClient` directly). The test is scoped
     * so a fix to either entry is a one-line removal.
     */
    private val preExistingAllowList: Set<String> = setOf(
        "SetupActivity.kt",
    )

    @Test
    fun `ui layer must not import http or dto types`() {
        val uiDir = File("src/main/kotlin/com/sense/relay/ui")
        if (!uiDir.exists()) {
            // Sanity: if the layout changed, fail loudly so the test
            // is updated to point at the new location.
            error("ui source directory not found at ${uiDir.absolutePath} — update this test")
        }
        val offenders = uiDir.walkTopDown()
            .filter { it.isFile && it.extension == "kt" }
            .filter { f -> f.name !in preExistingAllowList }
            .filter { f ->
                val text = f.readText()
                text.lineSequence().any { line ->
                    val trimmed = line.trimStart()
                    trimmed.startsWith("import com.sense.relay.http")
                }
            }
            .map { it.relativeTo(uiDir).path }
            .toList()

        assertTrue(
            "ui/ files must not import com.sense.relay.http.* (INV-11 UI guard):\n  ${offenders.joinToString("\n  ")}",
            offenders.isEmpty(),
        )
    }
}
