package com.opensapien.relay.core

import android.util.Log

/**
 * Trace-aware logging. Every Chat / Memory request logs ONE line carrying
 * the request_id, retrieval_trace_id, and audit_id so an operator can grep
 * one log file and follow a single user request from chat to backend to
 * audit row.
 *
 * Use [d] for debug, [w] for warnings, [e] for errors. The TraceContext
 * parameter is optional — without it the call still logs, just without
 * the trace fields.
 */
data class TraceContext(
    val requestId: String,
    val retrievalTraceId: String? = null,
    val auditId: String? = null,
)

object SenseLog {
    fun d(tag: String, msg: String, trace: TraceContext? = null) {
        Log.d(tag, format(msg, trace))
    }

    fun w(tag: String, msg: String, trace: TraceContext? = null) {
        Log.w(tag, format(msg, trace))
    }

    fun e(tag: String, msg: String, t: Throwable? = null, trace: TraceContext? = null) {
        if (t != null) {
            Log.e(tag, format(msg, trace), t)
        } else {
            Log.e(tag, format(msg, trace))
        }
    }

    internal fun format(msg: String, trace: TraceContext?): String {
        if (trace == null) return msg
        val sb = StringBuilder(msg)
        sb.append(" request_id=").append(trace.requestId)
        trace.retrievalTraceId?.let { sb.append(" trace_id=").append(it) }
        trace.auditId?.let { sb.append(" audit_id=").append(it) }
        return sb.toString()
    }
}
