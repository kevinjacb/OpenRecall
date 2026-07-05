package com.sense.relay.data

import android.app.Application
import com.sense.relay.relay.RelayController
import com.sense.relay.store.ServerConfig

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
     * device, server status, and the relay state controller itself
     * (exposed so screens can observe).
     */
    data class Repositories(
        val configuration: ConfigurationRepository,
        val session: SessionRepository,
        val device: DeviceRepository,
        val status: StatusRepository,
        val relayController: RelayController,
    )

    lateinit var repos: Repositories
        private set

    /**
     * Initialize the singleton from the application. Idempotent: a
     * second call is a no-op so a re-attach in instrumentation tests
     * doesn't double-construct.
     */
    fun init(app: Application) {
        if (::repos.isInitialized) return
        repos = Repositories(
            configuration = ConfigurationRepositoryImpl(ServerConfig(app.filesDir)),
            session = FakeSessionRepository(),
            device = DeviceRepositoryImpl(RelayController),
            status = NoopStatusRepository(),
            relayController = RelayController,
        )
    }
}
