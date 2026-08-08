package com.openrecall.relay.ui.recordings

import com.openrecall.relay.domain.model.SessionSummary

/**
 * The headline shown for a session on Home and in the Recordings list.
 *
 * The `design/` comp gives every session a human title ("Studio standup",
 * "Call with Priya"). The server has no such field — a session is an id, a
 * time range and a transcript — and nothing in the app derives one, so the
 * short id stands in. That keeps rows unambiguous and matches what the list
 * has always shown. Replace this one function if titling ever lands
 * server-side.
 */
fun sessionTitle(session: SessionSummary): String = "Session ${session.id.value.take(8)}"
