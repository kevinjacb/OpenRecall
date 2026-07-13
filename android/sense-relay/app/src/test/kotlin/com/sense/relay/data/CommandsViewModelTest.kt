package com.sense.relay.data

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class CommandsViewModelTest {

    @Test
    fun `refresh transitions Loading → Ready with commands`() = runTest {
        val repo = FakeCommandRepository(
            listResult = listOf(_command("c1", CommandStatus.PENDING)),
        )
        val vm = CommandsViewModel(repo)
        vm.refresh()
        assertTrue(vm.state.value is CommandsViewModel.UiState.Ready)
        val cmds = (vm.state.value as CommandsViewModel.UiState.Ready).commands
        assertEquals(1, cmds.size)
        assertEquals("c1", cmds[0].commandId)
    }

    @Test
    fun `refresh transitions Loading → Error on repo failure`() = runTest {
        val repo = FakeCommandRepository(throwOnList = true)
        val vm = CommandsViewModel(repo)
        vm.refresh()
        assertTrue(vm.state.value is CommandsViewModel.UiState.Error)
        val err = vm.state.value as CommandsViewModel.UiState.Error
        assertTrue(err.message.contains("network"))
    }

    @Test
    fun `ack transitions the matching command to EXECUTING`() = runTest {
        val c1 = _command("c1", CommandStatus.DELIVERED)
        val c2 = _command("c2", CommandStatus.PENDING)
        val repo = FakeCommandRepository(
            initialList = listOf(c1, c2),
        )
        val vm = CommandsViewModel(repo)
        vm.refresh()
        vm.ack("c1")
        val cmds = (vm.state.value as CommandsViewModel.UiState.Ready).commands
        assertEquals(CommandStatus.EXECUTING, cmds[0].status)
        assertEquals(CommandStatus.PENDING, cmds[1].status) // c2 unchanged
    }
}

private fun _command(
    commandId: String,
    status: CommandStatus,
): Command {
    return Command(
        commandId = commandId,
        sessionId = "s1",
        type = "capture_photo",
        params = emptyMap(),
        issuedAt = java.time.Instant.parse("2026-07-01T00:00:00Z"),
        expiresAt = java.time.Instant.parse("2026-07-01T00:30:00Z"),
        status = status,
    )
}

private class FakeCommandRepository(
    private val initialList: List<Command>? = null,
    private val listResult: List<Command>? = null,
    private val throwOnList: Boolean = false,
) : CommandRepository(StubCommandApi()) {
    override suspend fun listActive(): List<Command> {
        if (throwOnList) throw RuntimeException("network error")
        return listResult ?: initialList ?: emptyList()
    }
    override suspend fun get(commandId: String): Command {
        return (listResult ?: initialList ?: emptyList()).first { it.commandId == commandId }
    }
    override suspend fun ack(commandId: String): Command {
        val current = (listResult ?: initialList ?: emptyList())
            .first { it.commandId == commandId }
        // Simulate the server's behavior: ack moves DELIVERED → EXECUTING.
        return current.copy(
            status = if (current.status == CommandStatus.DELIVERED) CommandStatus.EXECUTING else current.status
        )
    }
}
