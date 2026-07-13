package com.sense.relay.data

import com.sense.relay.http.HttpApiError
import com.sense.relay.http.dto.CommandRecordDto
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update

/**
 * Domain layer for the command-lifecycle UI.
 *
 * Wraps [CommandApi] and exposes:
 * - listActive(): suspend → List<Command>
 * - get(id): suspend → Command
 * - ack(id): suspend → Command
 *
 * The repository is the only place that imports [HttpApiError] for the
 * command endpoints (mirrors AgentRepository's role for agent
 * endpoints). Callers see only domain types.
 */
open class CommandRepository(private val api: CommandApi) {

    open suspend fun listActive(): List<Command> =
        api.listActive().map { it.toDomain() }

    open suspend fun get(commandId: String): Command =
        api.get(commandId).toDomain()

    open suspend fun ack(commandId: String): Command =
        api.ack(commandId).toDomain()
}

/**
 * Mutable state holder for the CommandsScreen (P2-commands Phase 9).
 *
 * Exposes a single StateFlow<UiState> with the current list of
 * active commands and the lifecycle-loading state. The screen
 * collects this flow and re-renders.
 */
class CommandsViewModel(
    private val repo: CommandRepository,
) {
    sealed class UiState {
        data object Loading : UiState()
        data class Error(val message: String) : UiState()
        data class Ready(val commands: List<Command>) : UiState()
    }

    private val _state = MutableStateFlow<UiState>(UiState.Loading)
    val state: StateFlow<UiState> = _state.asStateFlow()

    suspend fun refresh() {
        _state.value = UiState.Loading
        try {
            val cmds = repo.listActive()
            _state.value = UiState.Ready(cmds)
        } catch (e: Exception) {
            _state.value = UiState.Error(e.message ?: "failed to load commands")
        }
    }

    suspend fun ack(commandId: String) {
        try {
            repo.ack(commandId)
            // After ack, the lifecycle status changed; refresh the list.
            _state.update { current ->
                if (current is UiState.Ready) {
                    UiState.Ready(current.commands.map { c ->
                        if (c.commandId == commandId) {
                            c.copy(status = CommandStatus.EXECUTING)
                        } else c
                    })
                } else current
            }
        } catch (e: Exception) {
            // Refresh anyway; the server may have applied the ack
            // but the response was malformed.
            refresh()
        }
    }
}
