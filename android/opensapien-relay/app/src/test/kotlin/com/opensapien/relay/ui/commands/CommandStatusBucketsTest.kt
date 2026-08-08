package com.opensapien.relay.ui.commands

import com.opensapien.relay.data.CommandStatus
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CommandStatusBucketsTest {

    @Test
    fun `isInFlight is true for PENDING VALIDATED ISSUED DELIVERED EXECUTING`() {
        val inFlight = listOf(
            CommandStatus.PENDING,
            CommandStatus.VALIDATED,
            CommandStatus.ISSUED,
            CommandStatus.DELIVERED,
            CommandStatus.EXECUTING,
        )
        inFlight.forEach { assertTrue("$it should be in-flight", it.isInFlight()) }
    }

    @Test
    fun `isInFlight is false for COMPLETED FAILED CANCELLED TIMED_OUT`() {
        val terminal = listOf(
            CommandStatus.COMPLETED,
            CommandStatus.FAILED,
            CommandStatus.CANCELLED,
            CommandStatus.TIMED_OUT,
        )
        terminal.forEach { assertFalse("$it should not be in-flight", it.isInFlight()) }
    }

    @Test
    fun `isTerminalFailure is true for FAILED CANCELLED TIMED_OUT`() {
        val failures = listOf(
            CommandStatus.FAILED,
            CommandStatus.CANCELLED,
            CommandStatus.TIMED_OUT,
        )
        failures.forEach { assertTrue("$it should be a terminal failure", it.isTerminalFailure()) }
    }

    @Test
    fun `isTerminalFailure is false for non-failure statuses`() {
        val nonFailures = listOf(
            CommandStatus.PENDING,
            CommandStatus.VALIDATED,
            CommandStatus.ISSUED,
            CommandStatus.DELIVERED,
            CommandStatus.EXECUTING,
            CommandStatus.COMPLETED,
        )
        nonFailures.forEach { assertFalse("$it should not be a terminal failure", it.isTerminalFailure()) }
    }
}
