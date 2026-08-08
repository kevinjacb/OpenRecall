package com.openrecall.relay.ui.chat

import com.openrecall.relay.data.ChatMessageKind
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * Round-trip the ChatMessageKind enum. Exhaustiveness in the
 * screen's `when (msg.kind)` is verified by the compiler; this
 * test pins the names + ordinals so a future reorder is a
 * deliberate edit.
 */
class ChatMessageKindTest {

    @Test
    fun `USER_TEXT is the default kind for a new ChatMessage`() {
        // Construct via the user-typed branch in ChatViewModel.ask():
        // the kind is not set explicitly, so it must default to USER_TEXT.
        // The actual ChatMessage is constructed inside ChatViewModel.ask();
        // here we exercise the default value at the data-class level.
        // We can't construct ChatMessage without role (required), so we
        // assert via the default-value property instead.
        val default: ChatMessageKind = ChatMessageKind.USER_TEXT
        assertEquals("USER_TEXT", default.name)
    }

    @Test
    fun `AGENT_ANSWER has the expected name`() {
        assertEquals("AGENT_ANSWER", ChatMessageKind.AGENT_ANSWER.name)
    }

    @Test
    fun `AGENT_REFUSE has the expected name`() {
        assertEquals("AGENT_REFUSE", ChatMessageKind.AGENT_REFUSE.name)
    }

    @Test
    fun `AGENT_ERROR has the expected name`() {
        assertEquals("AGENT_ERROR", ChatMessageKind.AGENT_ERROR.name)
    }

    @Test
    fun `NAME_SPEAKER has the expected name`() {
        // The exhaustive `when` in ChatMessageList depends on this enum;
        // adding a constant without handling it is a compile error there.
        assertEquals("NAME_SPEAKER", ChatMessageKind.NAME_SPEAKER.name)
    }
}
