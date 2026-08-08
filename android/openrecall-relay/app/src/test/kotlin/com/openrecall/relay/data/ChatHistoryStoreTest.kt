package com.openrecall.relay.data

import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Test

class ChatHistoryStoreTest {

    @Test
    fun `append adds to messages and increments unflushed`() = runTest {
        val s = ChatHistoryStore()
        s.append(ChatMessage(id = "1", role = Role.USER, text = "hi"))
        assertEquals(1, s.messages.value.size)
        assertEquals(1, s.unflushedCount())
    }

    @Test
    fun `replace updates the same id`() = runTest {
        val s = ChatHistoryStore()
        val pending = ChatMessage(id = "1", role = Role.AGENT, text = "...")
        s.append(pending)
        s.replace(pending, pending.copy(text = "answer"))
        assertEquals(1, s.messages.value.size)
        assertEquals("answer", s.messages.value[0].text)
    }

    @Test
    fun `clear empties the store`() = runTest {
        val s = ChatHistoryStore()
        s.append(ChatMessage(id = "1", role = Role.USER, text = "hi"))
        s.clear()
        assertEquals(0, s.messages.value.size)
        assertEquals(0, s.unflushedCount())
    }

    @Test
    fun `flush resets unflushed count`() = runTest {
        val s = ChatHistoryStore()
        s.append(ChatMessage(id = "1", role = Role.USER, text = "hi"))
        assertEquals(1, s.unflushedCount())
        s.flush()
        assertEquals(0, s.unflushedCount())
    }

    @Test
    fun `messages survive across replaces`() = runTest {
        val s = ChatHistoryStore()
        s.append(ChatMessage(id = "1", role = Role.USER, text = "first"))
        s.append(ChatMessage(id = "2", role = Role.AGENT, text = "answer1"))
        s.append(ChatMessage(id = "3", role = Role.USER, text = "second"))
        s.replace(s.messages.value[1], s.messages.value[1].copy(text = "answer1-final"))
        assertEquals(3, s.messages.value.size)
        assertEquals("answer1-final", s.messages.value[1].text)
        assertEquals("second", s.messages.value[2].text)
    }
}
