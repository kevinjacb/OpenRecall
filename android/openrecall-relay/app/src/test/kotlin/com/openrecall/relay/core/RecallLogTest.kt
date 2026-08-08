package com.openrecall.relay.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class RecallLogTest {

    @Test
    fun `format adds request_id when present`() {
        val out = RecallLog.format("hello", TraceContext("req-1"))
        assertEquals("hello request_id=req-1", out)
    }

    @Test
    fun `format adds trace_id and audit_id when present`() {
        val out = RecallLog.format(
            "hi",
            TraceContext("req-1", retrievalTraceId = "trace-1", auditId = "audit-1"),
        )
        assertTrue(out.contains("request_id=req-1"))
        assertTrue(out.contains("trace_id=trace-1"))
        assertTrue(out.contains("audit_id=audit-1"))
    }

    @Test
    fun `format omits optional fields when null`() {
        val out = RecallLog.format("hi", TraceContext("req-1"))
        assertTrue(!out.contains("trace_id="))
        assertTrue(!out.contains("audit_id="))
    }

    @Test
    fun `format passes through msg unchanged when no trace`() {
        val out = RecallLog.format("plain", null)
        assertEquals("plain", out)
    }

    @Test
    fun `trace_context_default_values_are_null`() {
        val t = TraceContext("req-1")
        assertEquals(null, t.retrievalTraceId)
        assertEquals(null, t.auditId)
    }
}
