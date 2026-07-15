package com.sense.relay.data

import androidx.lifecycle.ViewModel
import com.sense.relay.http.HttpApiError
import com.sense.relay.http.dto.CommandRecordDto
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.plus

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
    pollDispatcher: CoroutineDispatcher = Dispatchers.Main.immediate,
) : ViewModel() {
    sealed class UiState {
        data object Loading : UiState()
        data class Error(val message: String) : UiState()
        data class Ready(val commands: List<Command>) : UiState()
    }

    private val _state = MutableStateFlow<UiState>(UiState.Loading)
    val state: StateFlow<UiState> = _state.asStateFlow()

    /**
     * Dedicated scope for the polling job. We use a private
     * `SupervisorJob` + an injected [pollDispatcher] (defaulting to
     * `Dispatchers.Main.immediate`) rather than `viewModelScope`
     * so the polling loop is decoupled from the framework's
     * ViewModelStore ownership and is straightforward to control
     * in unit tests via the test dispatcher. The supervisor means
     * a failure in one tick does not cancel the loop. Cancelled
     * explicitly in [stopPolling] and [onCleared].
     */
    private val pollScope: CoroutineScope = CoroutineScope(SupervisorJob() + pollDispatcher)

    private var pollJob: Job? = null

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

    /**
     * Polling lifecycle (added for the CommandsScreen P2-commands
     * Phase 9 Composable). The screen calls this in a
     * LifecycleEventEffect(ON_RESUME) and `stopPolling()` in
     * ON_PAUSE. The job lives on a private [pollScope] (not
     * `viewModelScope`) so it is straightforward to test with
     * `runTest`. The explicit [onCleared] override cancels the
     * job and the scope on teardown.
     *
     * `startPolling` is idempotent: calling it twice cancels the
     * prior job before starting a new one. The first `refresh()`
     * call is immediate so the screen does not wait one full
     * interval for its first paint.
     */
    fun startPolling(intervalMs: Long = 4_000L) {
        pollJob?.cancel()
        pollJob = pollScope.launch {
            refresh()
            while (isActive) {
                delay(intervalMs)
                refresh()
            }
        }
    }

    fun stopPolling() {
        pollJob?.cancel()
        pollJob = null
    }

    override fun onCleared() {
        stopPolling()
        pollScope.coroutineContext[Job]?.cancel()
        super.onCleared()
    }
}
