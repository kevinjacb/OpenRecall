package com.openrecall.relay.http.dto

import com.openrecall.relay.domain.model.AudioSegment
import com.openrecall.relay.domain.model.CaptureEvent
import com.openrecall.relay.domain.model.TranscriptChunk
import com.openrecall.relay.protocol.Wire
import kotlinx.serialization.encodeToString
import java.time.Instant
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * Executable spec for the wire-DTO ↔ domain mapping. Each test
 * builds a JSON object by hand (the server's shape), decodes it with
 * `Wire.json`, and asserts the domain conversion. This is the
 * contract every repository and ViewModel relies on — if the server
 * shape changes, this test fails before the UI does.
 */
class MappersTest {

    @Test fun sessionSummaryDtoRoundTrip() {
        val json = """
            {"id":"s1","startedAt":"2026-07-04T10:00:00Z",
             "durationMs":60000,"transcriptCount":3,"preview":"hi"}
        """.trimIndent()
        val dto = Wire.json.decodeFromString(SessionSummaryDto.serializer(), json)
        val domain = dto.toDomain()
        assertEquals("s1", domain.id.value)
        assertEquals(Instant.parse("2026-07-04T10:00:00Z"), domain.startedAt)
        assertNull(domain.endedAt)
        assertEquals(60_000, domain.durationMs)
        assertEquals(3, domain.transcriptCount)
        assertEquals("hi", domain.preview)
    }

    @Test fun sessionSummaryDtoWithEndedAt() {
        val json = """
            {"id":"s1","startedAt":"2026-07-04T10:00:00Z","endedAt":"2026-07-04T10:01:00Z",
             "durationMs":60000,"transcriptCount":0,"preview":""}
        """.trimIndent()
        val domain = Wire.json.decodeFromString(SessionSummaryDto.serializer(), json).toDomain()
        assertEquals(Instant.parse("2026-07-04T10:01:00Z"), domain.endedAt)
    }

    @Test fun sessionSummaryDtoParsesServerOffsetWireFormat() {
        // The server emits an explicit `+00:00` offset — `datetime.isoformat()`
        // on a `timezone.utc` value — NOT a `Z` suffix (pinned server-side by
        // test_sessions.test_sessions_summary_shape_matches_dto: "…+00:00").
        // This contract test pins that the mapper accepts the server's actual
        // wire format. On the CI JVM `Instant.parse` happens to accept `+00:00`
        // too (JDK-8166138, fixed in Java 13), so this alone doesn't reproduce
        // the older-Android failure — but it guards the contract: if the mapper
        // ever regresses to a parser that rejects the offset, this fails. The
        // production parser is `OffsetDateTime.parse(s).toInstant()`, which
        // accepts `Z` and `+00:00` (and fractional seconds) on every version.
        val json = """
            {"id":"s1","startedAt":"2026-07-01T12:00:00+00:00",
             "durationMs":60000,"transcriptCount":1,"preview":"p"}
        """.trimIndent()
        val domain = Wire.json.decodeFromString(SessionSummaryDto.serializer(), json).toDomain()
        assertEquals(Instant.parse("2026-07-01T12:00:00Z"), domain.startedAt)
    }

    @Test fun sessionSummaryDtoParsesServerOffsetWithFractionalSeconds() {
        // `datetime.isoformat()` includes fractional seconds when microseconds
        // are non-zero: "…12:00:00.123456+00:00". The mapper must not fall back
        // to EPOCH for that — the most common real timestamp shape.
        val json = """
            {"id":"s2","startedAt":"2026-07-01T12:00:00.123456+00:00",
             "durationMs":1000,"transcriptCount":0,"preview":""}
        """.trimIndent()
        val domain = Wire.json.decodeFromString(SessionSummaryDto.serializer(), json).toDomain()
        assertEquals(Instant.parse("2026-07-01T12:00:00.123456Z"), domain.startedAt)
    }

    @Test fun sessionDetailsDtoMapsSummary() {
        val json = """
            {"summary":{"id":"s1","startedAt":"2026-07-04T10:00:00Z",
                        "durationMs":1000,"transcriptCount":0,"preview":""},
             "events":[]}
        """.trimIndent()
        val dto = Wire.json.decodeFromString(SessionDetailsDto.serializer(), json)
        val d = dto.toDomain()
        assertEquals("s1", d.summary.id.value)
        // Phase 2 mapper only maps the summary; events are produced
        // by `GET /sessions/{id}/events` via toDomain(List).
        assertTrue(d.events.isEmpty())
    }

    @Test fun captureEventTranscriptToDomain() {
        val json = """
            {"id":"e1","sessionId":"s1","seq":0,"startMs":0,
             "createdAt":"2026-07-04T10:00:00Z",
             "kind":"transcript","text":"hello","durationMs":1500}
        """.trimIndent()
        val dto = Wire.json.decodeFromString(CaptureEventDto.serializer(), json)
        var unknownCalls = 0
        val ev = dto.toDomainOrNull { unknownCalls++ }
        assertNotNull(ev)
        assertTrue(ev is TranscriptChunk)
        assertEquals("hello", ev.text)
        assertEquals(1500, ev.durationMs)
        assertEquals(0, unknownCalls)
    }

    @Test fun captureEventAudioToDomain() {
        val json = """
            {"id":"e2","sessionId":"s1","seq":1,"startMs":1000,
             "createdAt":"2026-07-04T10:00:01Z",
             "kind":"audio","codec":"opus","sampleRateHz":16000,"byteCount":4096}
        """.trimIndent()
        val dto = Wire.json.decodeFromString(CaptureEventDto.serializer(), json)
        var unknownCalls = 0
        val ev = dto.toDomainOrNull { unknownCalls++ }
        assertNotNull(ev)
        assertTrue(ev is AudioSegment)
        assertEquals("opus", ev.codec)
        assertEquals(16000, ev.sampleRateHz)
        assertEquals(4096, ev.byteCount)
        assertEquals(0, unknownCalls)
    }

    @Test fun captureEventUnknownKindIsDroppedAndWarns() {
        val json = """
            {"id":"e3","sessionId":"s1","seq":2,"startMs":2000,
             "createdAt":"2026-07-04T10:00:02Z","kind":"future_image"}
        """.trimIndent()
        val dto = Wire.json.decodeFromString(CaptureEventDto.serializer(), json)
        val unknown = mutableListOf<String>()
        val ev = dto.toDomainOrNull { unknown.add(it) }
        assertNull(ev)
        assertEquals(listOf("future_image"), unknown)
    }

    @Test fun captureEventListDropsUnknownKinds() {
        val json = """
            [
              {"id":"e1","sessionId":"s1","seq":0,"startMs":0,
               "createdAt":"2026-07-04T10:00:00Z","kind":"transcript",
               "text":"hi","durationMs":500},
              {"id":"e2","sessionId":"s1","seq":1,"startMs":500,
               "createdAt":"2026-07-04T10:00:00Z","kind":"future_x"}
            ]
        """.trimIndent()
        val dtos = Wire.json.decodeFromString(
            kotlinx.serialization.builtins.ListSerializer(CaptureEventDto.serializer()), json,
        )
        val unknown = mutableListOf<String>()
        val events: List<CaptureEvent> = dtos.toDomain { unknown.add(it) }
        assertEquals(1, events.size)
        assertTrue(events[0] is TranscriptChunk)
        assertEquals(listOf("future_x"), unknown)
    }

    @Test fun serverStatusDtoToDomain() {
        val json = """
            {"reachable":true,"authenticated":true,"version":"0.2.0",
             "uptimeSeconds":3600,"activeSessions":2,"totalSessions":17,
             "recentEvents24h":1200}
        """.trimIndent()
        val dto = Wire.json.decodeFromString(ServerStatusDto.serializer(), json)
        val s = dto.toDomain()
        assertTrue(s.reachable)
        assertTrue(s.authenticated)
        assertEquals("0.2.0", s.version)
        assertEquals(3600, s.uptimeSeconds)
        assertEquals(2, s.activeSessions)
        assertEquals(17, s.totalSessions)
        assertEquals(1200, s.recentEvents24h)
    }

    @Test fun sessionsPageDtoToDomain() {
        val json = """
            {"sessions":[
              {"id":"s1","startedAt":"2026-07-04T10:00:00Z",
               "durationMs":1000,"transcriptCount":0,"preview":""}
            ],"nextCursor":"cur-1"}
        """.trimIndent()
        val dto = Wire.json.decodeFromString(SessionsPageDto.serializer(), json)
        assertEquals(1, dto.sessions.size)
        assertEquals("cur-1", dto.nextCursor)
    }

    @Test fun encoderProducesExpectedJson() {
        // The mapper's output side — encode a DTO and parse it back,
        // assert the on-the-wire shape is what Phase 3's server will
        // expect. (Phase 2 doesn't post anything; this pins the shape
        // for when we do.)
        val dto = SessionSummaryDto(
            id = "s1", startedAt = "2026-07-04T10:00:00Z",
            durationMs = 1000, transcriptCount = 0, preview = "",
        )
        val encoded = Wire.json.encodeToString(dto)
        assertTrue(encoded.contains("\"id\":\"s1\""))
        assertTrue(encoded.contains("\"startedAt\":\"2026-07-04T10:00:00Z\""))
    }
}
