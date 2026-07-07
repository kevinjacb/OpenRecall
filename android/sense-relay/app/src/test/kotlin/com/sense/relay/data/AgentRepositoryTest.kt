package com.sense.relay.data

import com.sense.relay.data.AgentOutcomeKind
import com.sense.relay.http.ErrorCode
import com.sense.relay.http.HttpApiError
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class AgentRepositoryTest {

    @Test
    fun `maps return outcome to Answer with chips`() = runTest {
        val repo = AgentRepository(FakeAgentApi(
            response = AgentResponseSample(
                outcome = "return",
                answer = "x",
                atoms = listOf(AtomChipSample("a1", "hello")),
                confidence = 0.9,
                confidenceBand = "high",
            ),
        ))
        val out = repo.ask("s1", "what?")
        assertTrue(out is AgentOutcome.Answer)
        val ans = out as AgentOutcome.Answer
        assertEquals("x", ans.text)
        assertEquals(1, ans.atoms.size)
        assertEquals(AgentOutcomeKind.RETURN, ans.outcome)
        assertEquals("high", ans.confidenceBand)
    }

    @Test
    fun `maps return_with_uncertainty to Answer with correct kind`() = runTest {
        val repo = AgentRepository(FakeAgentApi(
            response = AgentResponseSample(
                outcome = "return_with_uncertainty",
                answer = "x",
                confidence = 0.7,
                confidenceBand = "medium",
            ),
        ))
        val out = repo.ask(null, "what?")
        assertTrue(out is AgentOutcome.Answer)
        assertEquals(AgentOutcomeKind.RETURN_WITH_UNCERTAINTY, (out as AgentOutcome.Answer).outcome)
    }

    @Test
    fun `maps refuse outcome to Refuse with reason`() = runTest {
        val repo = AgentRepository(FakeAgentApi(
            response = AgentResponseSample(
                outcome = "refuse",
                refusalReason = "no_supporting_memory",
            ),
        ))
        val out = repo.ask(null, "what?")
        assertTrue(out is AgentOutcome.Refuse)
        assertEquals("no_supporting_memory", (out as AgentOutcome.Refuse).reason)
    }

    @Test
    fun `maps HttpApiError to Error outcome`() = runTest {
        val repo = AgentRepository(FailingAgentApi())
        val out = repo.ask(null, "what?")
        assertTrue(out is AgentOutcome.Error)
        assertEquals(ErrorCode.INTERNAL_ERROR, (out as AgentOutcome.Error).code)
    }
}
