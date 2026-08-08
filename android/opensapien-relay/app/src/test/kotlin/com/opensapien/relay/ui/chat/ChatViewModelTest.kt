package com.opensapien.relay.ui.chat

import com.opensapien.relay.data.AtomChip
import com.opensapien.relay.data.AgentOutcome
import com.opensapien.relay.data.AgentOutcomeKind
import com.opensapien.relay.data.AgentRepository
import com.opensapien.relay.data.ChatHistoryStore
import com.opensapien.relay.data.ChatMessageKind
import com.opensapien.relay.data.Role
import com.opensapien.relay.data.SpeakerActions
import com.opensapien.relay.http.ErrorCode
import com.opensapien.relay.relay.RelayConnectionState
import com.opensapien.relay.relay.RelayController
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class ChatViewModelTest {

    private val dispatcher = StandardTestDispatcher()

    @Before
    fun setUp() {
        Dispatchers.setMain(dispatcher)
        // RelayController is a process singleton the VM reads imperatively
        // for the live session id; reset it between tests so a prior test's
        // Live connection never leaks into the next (mirrors DeviceViewModelTest).
        RelayController.reset()
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
                trace = com.opensapien.relay.core.TraceContext("r1", "t1", "a1"),
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
        assertEquals(ChatMessageKind.USER_TEXT, msgs[0].kind)
        assertEquals("what?", msgs[0].text)
        assertEquals(Role.AGENT, msgs[1].role)
        assertEquals(ChatMessageKind.AGENT_ANSWER, msgs[1].kind)
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
                trace = com.opensapien.relay.core.TraceContext("r1"),
            )
        }
        val store = ChatHistoryStore()
        val vm = ChatViewModel(repo, store)
        vm.onTextChanged("anything?")
        vm.ask()
        advanceUntilIdle()
        val msgs = store.messages.value
        assertEquals(2, msgs.size)
        assertEquals(ChatMessageKind.AGENT_REFUSE, msgs[1].kind)
        assertTrue(msgs[1].text.contains("No supporting", ignoreCase = true))
    }

    @Test
    fun `ask with error outcome shows error message`() = runTest(dispatcher) {
        val repo = FakeAgentRepo { _, _ ->
            AgentOutcome.Error(
                code = ErrorCode.INTERNAL_ERROR,
                message = "boom",
                trace = com.opensapien.relay.core.TraceContext("r1"),
            )
        }
        val store = ChatHistoryStore()
        val vm = ChatViewModel(repo, store)
        vm.onTextChanged("x")
        vm.ask()
        advanceUntilIdle()
        val msgs = store.messages.value
        assertEquals(2, msgs.size)
        assertEquals(ChatMessageKind.AGENT_ERROR, msgs[1].kind)
        assertTrue(msgs[1].text.contains("boom", ignoreCase = true))
    }

    @Test
    fun `clear empties the history`() = runTest(dispatcher) {
        val repo = FakeAgentRepo { _, _ ->
            AgentOutcome.Refuse("x", com.opensapien.relay.core.TraceContext("r"))
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

    @Test
    fun `nameSpeaker uses the live session id from RelayController`() = runTest(dispatcher) {
        val repo = FakeAgentRepo { _, _ -> error("ask should not be called") }
        val store = ChatHistoryStore()
        val actions = RecordingSpeakerActions()
        // The VM's default relayController is the RelayController singleton;
        // drive it Live so currentSessionId() resolves to the server session id
        // published by RelayService on socket open.
        RelayController.updateConnection(RelayConnectionState.Live("s-live", 0))
        val vm = ChatViewModel(repo, store, speakerActions = actions)

        vm.nameSpeaker("spk-1", "Sarah")
        advanceUntilIdle()

        assertEquals(1, actions.named.size)
        assertEquals("s-live", actions.named[0].sessionId)
        assertEquals("spk-1", actions.named[0].speakerId)
        assertEquals("Sarah", actions.named[0].name)
        // A successful send leaves no transient error.
        assertNull(vm.speakerError.value)
    }

    @Test
    fun `nameSpeaker surfaces a speakerError when the send fails`() = runTest(dispatcher) {
        val repo = FakeAgentRepo { _, _ -> error("ask should not be called") }
        val store = ChatHistoryStore()
        RelayController.updateConnection(RelayConnectionState.Live("s-live", 0))
        // A flaky link / disabled speaker-recognition surfaces as a thrown
        // error from SpeakerActions — the VM must NOT crash; it surfaces a
        // transient speakerError so the user knows the name wasn't saved.
        val vm = ChatViewModel(repo, store, speakerActions = ThrowingSpeakerActions())

        vm.nameSpeaker("spk-1", "Sarah")
        advanceUntilIdle()

        assertEquals("Couldn't save the name on the server", vm.speakerError.value)
        // And it can be cleared (the Snackbar dismiss path).
        vm.dismissSpeakerError()
        assertNull(vm.speakerError.value)
    }

    @Test
    fun `nameSpeaker noops when the relay is not live`() = runTest(dispatcher) {
        val repo = FakeAgentRepo { _, _ -> error("ask should not be called") }
        val store = ChatHistoryStore()
        val actions = RecordingSpeakerActions()
        // RelayController is Initial (reset in setUp) → currentSessionId() is null
        // → nameSpeaker must drop the request silently instead of crashing.
        val vm = ChatViewModel(repo, store, speakerActions = actions)

        vm.nameSpeaker("spk-1", "Sarah")
        advanceUntilIdle()

        assertEquals("no control emitted with no live session", 0, actions.named.size)
    }
}

private class FakeAgentRepo(
    private val onAsk: (sessionId: String?, text: String) -> AgentOutcome,
) : AgentRepository(api = FakeAgentApiForRepo()) {
    override suspend fun ask(sessionId: String?, text: String, limit: Int): AgentOutcome =
        onAsk(sessionId, text)
}

private class FakeAgentApiForRepo : com.opensapien.relay.data.AgentApi(
    baseUrl = "http://test",
    token = "t",
) {
    override suspend fun postAgent(
        sessionId: String?,
        text: String,
        limit: Int,
    ): com.opensapien.relay.http.dto.AgentResponseDto = error("not used in chat vm tests")
}

/** Captures nameSpeaker/reassignSpeaker calls so the VM tests can assert the
 *  control was emitted with the live session id — without a socket. Mirrors
 *  SessionDetailViewModelTest's RecordingSpeakerActions. */
private class RecordingSpeakerActions : SpeakerActions {
    data class Named(val sessionId: String, val speakerId: String, val name: String)

    val named = mutableListOf<Named>()

    override suspend fun nameSpeaker(sessionId: String, speakerId: String, name: String) {
        named += Named(sessionId, speakerId, name)
    }

    override suspend fun reassignSpeaker(sessionId: String, fromId: String, toId: String, scope: String) {
        // Not exercised by ChatViewModel; no-op capture.
    }
}

/** A [SpeakerActions] whose nameSpeaker always throws, to exercise the
 *  speakerError surfacing path (a failed HTTP rename). */
private class ThrowingSpeakerActions : SpeakerActions {
    override suspend fun nameSpeaker(sessionId: String, speakerId: String, name: String) {
        throw RuntimeException("boom")
    }

    override suspend fun reassignSpeaker(sessionId: String, fromId: String, toId: String, scope: String) {
        // Not exercised here.
    }
}
