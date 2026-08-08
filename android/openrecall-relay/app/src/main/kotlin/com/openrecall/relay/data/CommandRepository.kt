package com.openrecall.relay.data

import androidx.lifecycle.ViewModel
import com.openrecall.relay.http.HttpApiError
import com.openrecall.relay.http.dto.CommandRecordDto
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
 *
 * [apiProvider] is a suspend factory that returns the current
 * [CommandApi]. It exists so the repository tracks the latest
 * configured server (the `CommandApi` carries the OkHttp client +
 * bearer token at construction time). The repository resolves the
 * current `CommandApi` on every call, so re-provisioning takes
 * effect on the next request without rebuilding the repository.
 */
open class CommandRepository(private val apiProvider: suspend () -> CommandApi) {

    /** Test/convenience constructor: a repository pinned to a single [CommandApi]. */
    constructor(api: CommandApi) : this(apiProvider = { api })

    open suspend fun listActive(): List<Command> =
        apiProvider().listActive().map { it.toDomain() }

    open suspend fun get(commandId: String): Command =
        apiProvider().get(commandId).toDomain()

    open suspend fun ack(commandId: String): Command =
        apiProvider().ack(commandId).toDomain()

    /**
     * Issue a command to the wearable. [idempotencyKey] must be stable across
     * retries of the same intent — the server dedupes on it, so a fresh key
     * on a retry issues the command twice.
     */
    open suspend fun create(
        type: String,
        idempotencyKey: String,
        params: Map<String, String> = emptyMap(),
    ): Command = apiProvider().create(type, idempotencyKey, params).toDomain()
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

    private val _isRefreshing = MutableStateFlow(false)
    /** True while a pull-to-refresh re-fetch is in flight; drives the spinner. */
    val isRefreshing: StateFlow<Boolean> = _isRefreshing.asStateFlow()

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

    /**
     * Pull-to-refresh entry point (non-suspend, safe to call from a Composable
     * callback). Launches [refresh] on [pollScope] and toggles [isRefreshing]
     * around it so the [androidx.compose.material3.pulltorefresh.PullToRefreshBox]
     * spinner shows for the gesture. No-op if a refresh is already in flight.
     */
    fun onRefresh() {
        if (_isRefreshing.value) return
        _isRefreshing.value = true
        pollScope.launch {
            try {
                refresh()
            } finally {
                _isRefreshing.value = false
            }
        }
    }

    private val _issueError = MutableStateFlow<String?>(null)
    /** Non-null when the last [issue] was rejected. A guardrail refusal is a
     *  legitimate answer ("the device can't do that right now"), so it has to
     *  be shown rather than swallowed. */
    val issueError: StateFlow<String?> = _issueError.asStateFlow()

    /**
     * Issue a command to the wearable.
     *
     * The idempotency key is minted per tap: one tap is one intent, and the
     * server dedupes only within an unacked window, so reusing a key across
     * taps would silently drop the second one.
     *
     * `session_id` is deliberately not supplied — the gateway stamps the live
     * session at delivery, and an unbound command is simply queued until the
     * device next connects.
     */
    fun issue(type: String) {
        pollScope.launch {
            try {
                repo.create(type = type, idempotencyKey = java.util.UUID.randomUUID().toString())
                _issueError.value = null
                refresh()
            } catch (e: Exception) {
                _issueError.value = e.message ?: "Couldn't send that command"
            }
        }
    }

    fun dismissIssueError() {
        _issueError.value = null
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
