package com.openrecall.relay.ui.nav

import com.openrecall.relay.domain.model.SessionId
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNull

/**
 * SessionIdNavType is the type-safe bridge between Nav Compose's Bundle-based
 * arg system and the value-class SessionId. The deep-link entry point is
 * [SessionIdNavType.parseValue] (string in, SessionId out); the in-process
 * round-trip is [SessionIdNavType.get]/[SessionIdNavType.put] on a Bundle
 * (covered by instrumentation tests on a device — the host JVM Bundle is
 * a stub that throws on use).
 */
class NavTypeTest {

    @Test fun parseValueAcceptsArbitraryNonEmpty() {
        // The wire format is opaque — non-empty strings are valid SessionIds
        // at this layer. Server-side validation happens later.
        val back = SessionIdNavType.parseValue("01HMK9C5R3X4Y8K2QF1RT3JXVA")
        assertEquals(SessionId("01HMK9C5R3X4Y8K2QF1RT3JXVA"), back)
    }

    @Test fun parseValueAcceptsSimple() {
        assertEquals(SessionId("xyz"), SessionIdNavType.parseValue("xyz"))
    }

    @Test fun parseValueRejectsEmpty() {
        // Empty is the most common bad input (e.g. a missing arg, or a
        // string-typed slot that wasn't filled). NavType's contract is to
        // throw on invalid input — the framework catches and fails soft.
        assertFailsWith<IllegalArgumentException> {
            SessionIdNavType.parseValue("")
        }
    }

    @Test fun parseValueAndValueFieldAreEquivalent() {
        // Round-trip via the value field. The Bundle path (put/get) is
        // covered on-device; here we just pin the parseValue contract
        // against the SessionId value class.
        val s = "abc123"
        val parsed = SessionIdNavType.parseValue(s)
        assertEquals(SessionId(s), parsed)
        assertEquals(s, parsed?.value)
    }

    @Test fun nameIsStable() {
        // The `name` is part of the wire format (e.g. for back-stack
        // serialization); don't drift it without a deliberate bump.
        assertEquals("session_id", SessionIdNavType.name)
    }

    @Test fun isNotNullable() {
        // The framework reads isNullableAllowed to decide whether missing
        // args are valid. We don't want nullable session ids.
        assertEquals(false, SessionIdNavType.isNullableAllowed)
    }
}
