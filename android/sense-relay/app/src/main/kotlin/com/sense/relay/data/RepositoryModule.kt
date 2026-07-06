package com.sense.relay.data

import android.app.Application
import androidx.lifecycle.ProcessLifecycleOwner
import com.sense.relay.core.result.Outcome
import com.sense.relay.domain.model.ServerStatus
import com.sense.relay.domain.model.SessionId
import com.sense.relay.http.SenseHttpClient
import com.sense.relay.http.dto.CaptureEventDto
import com.sense.relay.http.dto.SessionDetailsDto
import com.sense.relay.http.dto.SessionsPageDto
import com.sense.relay.http.dto.toDomain
import com.sense.relay.relay.RelayController
import com.sense.relay.store.ServerConfig
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.io.IOException

/**
 * Process-singleton wiring. Constructed once in
 * [com.sense.relay.SenseApplication.onCreate], consumed by ViewModels
 * and screens.
 *
 * Manual DI by design: this slice is small, Hilt/Koin would add a
 * build tax without buying anything. Repositories that need
 * configuration take an `appContext` in [init]; consumers read
 * `repos.foo` directly.
 */
object RepositoryModule {

    /**
     * The aggregated repository surface. Holds every cross-cutting
     * dependency the rest of the app reads: configuration, sessions,
     * device, server status, the aggregated dashboard, and the relay
     * state controller itself (exposed so screens can observe).
     */
    data class Repositories(
        val configuration: ConfigurationRepository,
        val session: SessionRepository,
        val device: DeviceRepository,
        val status: StatusRepository,
        val dashboard: DashboardRepository,
        val relayController: RelayController,
    )

    lateinit var repos: Repositories
        private set

    /**
     * Initialize the singleton from the application. Idempotent: a
     * second call is a no-op so a re-attach in instrumentation tests
     * doesn't double-construct.
     *
     * The [PollingStatusRepository] observes the process lifecycle so
     * `/status` is polled only while the app is foregrounded, and the
     * [DashboardRepositoryImpl] fans the relay/status/session sources
     * into the Home screen's state — both share one process-scoped
     * [CoroutineScope].
     */
    fun init(app: Application) {
        if (::repos.isInitialized) return
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
        val configuration = ConfigurationRepositoryImpl(ServerConfig(app.filesDir))
        // One shared, config-aware, thread-safe client provider backs both
        // the status poller and the session API, so there is a single OkHttp
        // client per (url, token) across the app.
        val clientProvider = clientProvider(configuration)
        val session = SessionRepositoryImpl(HttpSessionApi(clientProvider))
        val status = PollingStatusRepository(
            fetch = statusFetch(clientProvider),
            lifecycle = ProcessLifecycleOwner.get().lifecycle,
            scope = scope,
        )
        val dashboard = DashboardRepositoryImpl(
            relayController = RelayController,
            statusRepo = status,
            sessionRepo = session,
            scope = scope,
        )
        repos = Repositories(
            configuration = configuration,
            session = session,
            device = DeviceRepositoryImpl(RelayController),
            status = status,
            dashboard = dashboard,
            relayController = RelayController,
        )
    }
}

/**
 * A config-aware, thread-safe provider of the current [SenseHttpClient].
 * Each call reads the persisted [com.sense.relay.store.Config] (so a
 * re-provision in Settings takes effect on the next call without re-wiring)
 * and returns a client memoized by (url, token) — so the OkHttp stack is
 * built once per credential set, not per request.
 *
 * **Not provisioned → `throw IOException`** (rather than returning null), so
 * the callers' existing catch/`httpApiError` paths turn it into
 * `Outcome.Failure` / `PagedResult.Error` uniformly.
 *
 * The `config.observe().first()` read and the memo are inside the guard:
 * `ServerConfig.observe()` is a raw DataStore `store.data` flow with no
 * `.catch`, so a read failure throws — callers catch it. The [Mutex] makes
 * the memoized `cached` safe to share across the status poller and the
 * session repository (which can call concurrently). When (url, token)
 * changes, the previous client is dropped without an explicit OkHttp
 * shutdown; its idle threads/sockets are reaped by OkHttp's idle timeout
 * (re-provision is infrequent, so the bounded idle-out is accepted).
 */
private fun clientProvider(config: ConfigurationRepository): suspend () -> SenseHttpClient {
    val mutex = Mutex()
    var cached: Pair<Pair<String, String>, SenseHttpClient>? = null
    return {
        val c = config.observe().first()
        if (!c.provisioned || c.serverUrl.isBlank()) {
            throw IOException("not provisioned")
        }
        val key = c.serverUrl to c.token
        mutex.withLock {
            cached?.takeIf { it.first == key }?.second
                ?: SenseHttpClient(c.serverUrl, c.token).also { cached = key to it }
        }
    }
}

/**
 * Production `/status` fetcher for [PollingStatusRepository]. Delegates to
 * the shared [clientProvider]; every throwable (including a "not
 * provisioned" or a DataStore read failure surfaced by the provider) is
 * caught and classified by [httpApiError] into an `Outcome.Failure`, so a
 * fetch never throws into the poll loop (the Phase-4 crash lesson).
 * Cancellation propagates.
 */
private fun statusFetch(client: suspend () -> SenseHttpClient): suspend () -> Outcome<ServerStatus> = {
    try {
        Outcome.Success(client().getStatus().toDomain())
    } catch (e: CancellationException) {
        throw e
    } catch (e: Throwable) {
        Outcome.Failure(httpApiError(e))
    }
}

/**
 * Production [SessionApi] over the shared [clientProvider]. Each method
 * resolves the current client (or throws "not provisioned", which the
 * repository catches) and delegates. The repository owns DTO → domain
 * mapping and error classification.
 */
private class HttpSessionApi(private val client: suspend () -> SenseHttpClient) : SessionApi {
    override suspend fun listSessions(limit: Int, cursor: String?): SessionsPageDto =
        client().listSessions(limit, cursor)

    override suspend fun getSession(id: SessionId): SessionDetailsDto =
        client().getSession(id)

    override suspend fun getSessionEvents(id: SessionId): List<CaptureEventDto> =
        client().getSessionEvents(id)
}
