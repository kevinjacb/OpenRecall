package com.openrecall.relay.data

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class CommandsViewModelTest {

    private val dispatcher = StandardTestDispatcher()

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

    @Test
    fun `onRefresh toggles isRefreshing and re-fetches the list`() = runTest(dispatcher) {
        // onRefresh is the non-suspend pull-to-refresh entry point: it sets
        // isRefreshing, launches refresh() on the poll scope, and clears the
        // flag when the fetch settles. The poll scope runs on the injected
        // dispatcher, so we drive it on the test scheduler.
        kotlinx.coroutines.Dispatchers.setMain(dispatcher)
        try {
            val repo = FakeCommandRepository(
                listResult = listOf(_command("c1", CommandStatus.PENDING)),
            )
            val vm = CommandsViewModel(repo, pollDispatcher = dispatcher)
            assertFalse(vm.isRefreshing.value, "not refreshing before the gesture")

            vm.onRefresh()
            assertTrue(vm.isRefreshing.value, "isRefreshing set immediately")
            testScheduler.advanceUntilIdle()

            assertTrue(vm.state.value is CommandsViewModel.UiState.Ready, "list re-fetched")
            assertFalse(vm.isRefreshing.value, "isRefreshing cleared after the fetch")
        } finally {
            kotlinx.coroutines.Dispatchers.resetMain()
        }
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
