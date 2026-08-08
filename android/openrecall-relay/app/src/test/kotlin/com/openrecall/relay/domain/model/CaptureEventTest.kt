package com.openrecall.relay.domain.model

import java.time.Instant
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/**
 * The sealed [CaptureEvent] hierarchy is the spec for every UI that
 * renders a session timeline. If a new subtype is added, this test
 * fails until the matching `when` arm is also added — that is the
 * exhaustiveness contract every consumer (ViewModel, mapper, UI)
 * relies on.
 */
class CaptureEventTest {

    private val t0 = Instant.parse("2026-07-04T10:00:00Z")
    private val sid = SessionId("s1")

    @Test fun transcriptChunkIsACaptureEvent() {
        val c: CaptureEvent = TranscriptChunk(
            id = "t1", sessionId = sid, seq = 0, startMs = 0, createdAt = t0,
            text = "hi", durationMs = 1500,
        )
        assertTrue(c is TranscriptChunk)
        assertEquals("t1", c.id)
        assertEquals(sid, c.sessionId)
        assertEquals(0, c.seq)
        assertEquals(0L, c.startMs)
        assertEquals(t0, c.createdAt)
        assertEquals("hi", c.text)
        assertEquals(1500, c.durationMs)
    }

    @Test fun audioSegmentIsACaptureEvent() {
        val a: CaptureEvent = AudioSegment(
            id = "a1", sessionId = sid, seq = 1, startMs = 100, createdAt = t0,
            codec = "opus", sampleRateHz = 16000, byteCount = 4096,
        )
        assertTrue(a is AudioSegment)
        assertEquals("a1", a.id)
        assertEquals("opus", a.codec)
        assertEquals(16000, a.sampleRateHz)
        assertEquals(4096, a.byteCount)
    }

    @Test fun sealedSubtypesAreExhaustive() {
        // Build one of each subtype, then exhaustively `when` on them. If a
        // new subtype is added, this test fails at the `when` because the
        // compiler will require a new branch.
        val events: List<CaptureEvent> = listOf(
            TranscriptChunk("t", sid, 0, 0L, t0, "x", 1),
            AudioSegment("a", sid, 0, 0L, t0, "opus", 16000, 1),
        )
        val kinds = events.map {
            when (it) {
                is TranscriptChunk -> "transcript"
                is AudioSegment -> "audio"
            }
        }
        assertEquals(listOf("transcript", "audio"), kinds)
    }

    @Test fun sharedFieldsAreEqualForSameInput() {
        val a = TranscriptChunk("t", sid, 7, 500, t0, "hi", 1000)
        val b = TranscriptChunk("t", sid, 7, 500, t0, "hi", 1000)
        assertEquals(a, b)
    }
}
