package com.opensapien.relay.ui.chat

import com.opensapien.relay.data.AtomChip
import com.opensapien.relay.data.ChatMessage
import com.opensapien.relay.data.ChatMessageKind
import com.opensapien.relay.data.Role
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Test

/**
 * Signature check for ProactiveMessageBubble. The actual Compose
 * rendering is validated manually on a real device (Robolectric
 * PR #4736 blocks host-JVM Compose UI tests in this sandbox).
 *
 * This test pins:
 * - The Composable exists with the documented signature.
 * - A ChatMessage of kind AGENT_PROACTIVE has the expected fields.
 * - The new AGENT_PROACTIVE variant is wired into the ChatMessageKind
 *   enum (proves the dispatch branch in ChatMessageList will compile).
 */
class ProactiveMessageBubbleTest {

    @Test
    fun `ChatMessage of kind AGENT_PROACTIVE carries text and atoms`() {
        val msg = ChatMessage(
            id = "m1",
            role = Role.AGENT,
            kind = ChatMessageKind.AGENT_PROACTIVE,
            text = "Here's what I noticed",
            atoms = listOf(
                AtomChip(
                    atomId = "a1",
                    sessionId = "s1",
                    kind = "fact",
                    text = "hello",
                    createdAt = "2026-07-19T12:00:00Z",
                    startMs = 0,
                    score = 0.9,
                ),
            ),
            traceRequestId = "r1",
            traceRetrievalId = "t1",
            traceAuditId = "a1",
        )
        assertEquals(ChatMessageKind.AGENT_PROACTIVE, msg.kind)
        assertEquals("Here's what I noticed", msg.text)
        assertEquals(1, msg.atoms.size)
    }

    @Test
    fun `ChatMessageKind has AGENT_PROACTIVE variant`() {
        // Exhaustiveness: this is the test. If AGENT_PROACTIVE is
        // missing from the enum, this won't compile.
        val kinds = ChatMessageKind.values().toList()
        assertNotNull(kinds.find { it == ChatMessageKind.AGENT_PROACTIVE })
    }

    @Test
    fun `ProactiveMessageBubble function exists with the expected signature`() {
        // The function must be top-level, in package
        // com.opensapien.relay.ui.chat, named ProactiveMessageBubble, taking
        // a ChatMessage as its first parameter. The simplest JVM-only
        // way to assert this is reflectively — the actual Compose
        // rendering is manual on a real device per the Robolectric
        // constraint. (The parameter count includes Compose's
        // synthetic $composer / $changed params, so we don't pin it.)
        val fn = Class.forName("com.opensapien.relay.ui.chat.ProactiveMessageBubbleKt")
            .declaredMethods
            .single { it.name == "ProactiveMessageBubble" }
        assertNotNull(fn)
        assertEquals(
            "com.opensapien.relay.data.ChatMessage",
            fn.parameterTypes[0].name,
        )
    }
}
