package com.openrecall.relay.core

import org.junit.Test
import kotlin.test.assertTrue

/**
 * Architectural invariant: the TraceContext data class is the only seam
 * for "carry request_id + retrieval_trace_id + audit_id together". Every
 * component that observes a Planner run uses this exact shape.
 */
class TraceContextTest {

    @Test
    fun `TraceContext carries all three ids when set`() {
        val t = TraceContext("r1", "t1", "a1")
        assertTrue(t.requestId == "r1")
        assertTrue(t.retrievalTraceId == "t1")
        assertTrue(t.auditId == "a1")
    }

    @Test
    fun `TraceContext allows null for partial trace`() {
        val t = TraceContext("r1")
        assertTrue(t.retrievalTraceId == null)
        assertTrue(t.auditId == null)
    }
}
