package com.openrecall.relay.ui.nav

import com.openrecall.relay.domain.model.SegmentId
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNull

/**
 * SegmentIdNavType is the type-safe bridge between Nav Compose's Bundle-based
 * arg system and the value-class SegmentId. The deep-link entry point is
 * [SegmentIdNavType.parseValue] (string in, SessionId out); the in-process
 * round-trip is [SegmentIdNavType.get]/[SegmentIdNavType.put] on a Bundle
 * (covered by instrumentation tests on a device — the host JVM Bundle is
 * a stub that throws on use).
 */
class NavTypeTest {

    @Test fun parseValueAcceptsArbitraryNonEmpty() {
        // The wire format is opaque — non-empty strings are valid SegmentIds
        // at this layer. Server-side validation happens later.
        val back = SegmentIdNavType.parseValue("01HMK9C5R3X4Y8K2QF1RT3JXVA:412")
        assertEquals(SegmentId("01HMK9C5R3X4Y8K2QF1RT3JXVA:412"), back)
    }

    @Test fun parseValueAcceptsSimple() {
        assertEquals(SegmentId("xyz"), SegmentIdNavType.parseValue("xyz"))
    }

    @Test fun parseValueRejectsEmpty() {
        // Empty is the most common bad input (e.g. a missing arg, or a
        // string-typed slot that wasn't filled). NavType's contract is to
        // throw on invalid input — the framework catches and fails soft.
        assertFailsWith<IllegalArgumentException> {
            SegmentIdNavType.parseValue("")
        }
    }

    @Test fun parseValueAndValueFieldAreEquivalent() {
        // Round-trip via the value field. The Bundle path (put/get) is
        // covered on-device; here we just pin the parseValue contract
        // against the SegmentId value class.
        val s = "abc123"
        val parsed = SegmentIdNavType.parseValue(s)
        assertEquals(SegmentId(s), parsed)
        assertEquals(s, parsed?.value)
    }

    @Test fun nameIsStable() {
        // The `name` is part of the wire format (e.g. for back-stack
        // serialization); don't drift it without a deliberate bump.
        assertEquals("segment_id", SegmentIdNavType.name)
    }

    @Test fun isNotNullable() {
        // The framework reads isNullableAllowed to decide whether missing
        // args are valid. We don't want nullable segment ids.
        assertEquals(false, SegmentIdNavType.isNullableAllowed)
    }
}
