package com.sense.relay.ui.chat

import com.sense.relay.data.AtomChip
import com.sense.relay.data.AgentOutcome
import com.sense.relay.data.AgentOutcomeKind
import com.sense.relay.data.AgentRepository
import com.sense.relay.data.ChatHistoryStore
import com.sense.relay.data.Role
import com.sense.relay.http.ErrorCode
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class ChatViewModelTest {

    private val dispatcher = StandardTestDispatcher()

    @Before
    fun setUp() {
        Dispatchers.setMain(dispatcher)
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    @Test
    fun `ask sends user message and receives answer`() = runTest(dispatcher) {
        val repo = FakeAgentRepo { sessionId, text ->
            AgentOutcome.Answer(
                text = "answer: $text",
                atoms = listOf(AtomChip("a1", "s1", "fact", "x", "2026-07-07", 0, 0.9)),
                confidence = 0.9,
                confidenceBand = "high",
                outcome = AgentOutcomeKind.RETURN,
                trace = com.sense.relay.core.TraceContext("r1", "t1", "a1"),
            )
        }
        val store = ChatHistoryStore()
        val vm = ChatViewModel(repo, store)
        vm.onTextChanged("what?")
        vm.ask()
        advanceUntilIdle()
        val msgs = store.messages.value
        assertEquals(2, msgs.size)
        assertEquals(Role.USER, msgs[0].role)
        assertEquals("what?", msgs[0].text)
        assertEquals(Role.AGENT, msgs[1].role)
        assertEquals("answer: what?", msgs[1].text)
        assertEquals(1, msgs[1].atoms.size)
    }

    @Test
    fun `ask with blank text is rejected`() = runTest(dispatcher) {
        val repo = FakeAgentRepo { _, _ -> error("should not be called") }
        val store = ChatHistoryStore()
        val vm = ChatViewModel(repo, store)
        vm.ask()
        advanceUntilIdle()
        assertEquals(0, store.messages.value.size)
    }

    @Test
    fun `ask with refuse outcome renders refusal message`() = runTest(dispatcher) {
        val repo = FakeAgentRepo { _, _ ->
            AgentOutcome.Refuse(
                reason = "no supporting memory",
                trace = com.sense.relay.core.TraceContext("r1"),
            )
        }
        val store = ChatHistoryStore()
        val vm = ChatViewModel(repo, store)
        vm.onTextChanged("anything?")
        vm.ask()
        advanceUntilIdle()
        val msgs = store.messages.value
        assertEquals(2, msgs.size)
        assertTrue(msgs[1].text.contains("No supporting", ignoreCase = true))
    }

    @Test
    fun `ask with error outcome shows error message`() = runTest(dispatcher) {
        val repo = FakeAgentRepo { _, _ ->
            AgentOutcome.Error(
                code = ErrorCode.INTERNAL_ERROR,
                message = "boom",
                trace = com.sense.relay.core.TraceContext("r1"),
            )
        }
        val store = ChatHistoryStore()
        val vm = ChatViewModel(repo, store)
        vm.onTextChanged("x")
        vm.ask()
        advanceUntilIdle()
        val msgs = store.messages.value
        assertEquals(2, msgs.size)
        assertTrue(msgs[1].text.contains("boom", ignoreCase = true))
    }

    @Test
    fun `clear empties the history`() = runTest(dispatcher) {
        val repo = FakeAgentRepo { _, _ ->
            AgentOutcome.Refuse("x", com.sense.relay.core.TraceContext("r"))
        }
        val store = ChatHistoryStore()
        val vm = ChatViewModel(repo, store)
        vm.onTextChanged("hi")
        vm.ask()
        advanceUntilIdle()
        assertEquals(2, store.messages.value.size)
        vm.clear()
        assertEquals(0, store.messages.value.size)
    }
}

private class FakeAgentRepo(
    private val onAsk: (sessionId: String?, text: String) -> AgentOutcome,
) : AgentRepository(api = FakeAgentApiForRepo()) {
    override suspend fun ask(sessionId: String?, text: String, limit: Int): AgentOutcome =
        onAsk(sessionId, text)
}

private class FakeAgentApiForRepo : com.sense.relay.data.AgentApi(
    baseUrl = "http://test",
    token = "t",
) {
    override suspend fun postAgent(
        sessionId: String?,
        text: String,
        limit: Int,
    ): com.sense.relay.http.dto.AgentResponseDto = error("not used in chat vm tests")
}
