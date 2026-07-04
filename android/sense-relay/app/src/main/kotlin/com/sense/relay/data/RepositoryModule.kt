package com.sense.relay.data

/**
 * Process-singleton wiring. The `Repositories` payload is empty in Phase 1;
 * later phases fill it in (ConfigurationRepository, SessionRepository,
 * DeviceRepository, StatusRepository, DashboardRepository). Constructed
 * once in [com.sense.relay.SenseApplication.onCreate], consumed by
 * ViewModels and screens.
 *
 * Manual DI by design: this slice is small, Hilt/Koin would add a build
 * tax without buying anything. Repositories that need configuration pass
 * an `appContext` in [init]; consumers read `repos.foo` directly.
 */
object RepositoryModule {

    /**
     * The aggregated repository surface. Empty in Phase 1 by design;
     * Phase 2 promotes this to a `data class` with the real repository
     * fields (ConfigurationRepository, SessionRepository,
     * DeviceRepository, StatusRepository, DashboardRepository) and
     * ViewModels/screens start consuming `repos.foo`.
     */
    class Repositories

    lateinit var repos: Repositories
        private set

    /** Initialize the singleton from the application. Idempotent: a
     *  second call is a no-op so a re-attach in instrumentation tests
     *  doesn't double-construct. */
    fun init(@Suppress("UNUSED_PARAMETER") app: android.app.Application) {
        if (::repos.isInitialized) return
        repos = Repositories()
    }
}
