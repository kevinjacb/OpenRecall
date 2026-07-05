package com.sense.relay.data

import android.app.Application
import androidx.lifecycle.ProcessLifecycleOwner
import com.sense.relay.core.model.ApiError
import com.sense.relay.core.result.Outcome
import com.sense.relay.domain.model.ServerStatus
import com.sense.relay.http.SenseHttpClient
import com.sense.relay.http.dto.toDomain
import com.sense.relay.relay.RelayController
import com.sense.relay.store.ServerConfig
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.first

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
        val session = FakeSessionRepository()
        val status = PollingStatusRepository(
            fetch = statusFetch(configuration),
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
 * Build the production `/status` fetcher for [PollingStatusRepository].
 * Each poll reads the current persisted [com.sense.relay.store.Config]
 * (so a re-provision in Settings takes effect on the next poll without
 * re-wiring), and — when provisioned — calls `GET /status` through a
 * [SenseHttpClient] memoized by (url, token) so we don't rebuild the
 * OkHttp stack every 2 seconds. Errors are classified by [statusApiError];
 * cancellation propagates.
 *
 * The captured `cached` client is safe without synchronization because the
 * poll loop invokes the returned lambda sequentially (one fetch completes
 * before the next begins).
 */
private fun statusFetch(config: ConfigurationRepository): suspend () -> Outcome<ServerStatus> {
    var cached: Pair<Pair<String, String>, SenseHttpClient>? = null
    fun clientFor(url: String, token: String): SenseHttpClient {
        val key = url to token
        cached?.let { if (it.first == key) return it.second }
        return SenseHttpClient(url, token).also { cached = key to it }
    }
    return fetch@{
        val c = config.observe().first()
        if (!c.provisioned || c.serverUrl.isBlank()) {
            return@fetch Outcome.Failure(ApiError.Unreachable("not provisioned"))
        }
        try {
            Outcome.Success(clientFor(c.serverUrl, c.token).getStatus().toDomain())
        } catch (e: CancellationException) {
            throw e
        } catch (e: Throwable) {
            Outcome.Failure(statusApiError(e))
        }
    }
}
