package com.sense.relay

import android.app.Application
import com.sense.relay.data.RepositoryModule

/**
 * Application entry point. Constructs the [RepositoryModule] singleton
 * here so it's ready before any Activity (or the relay service) reads
 * it. The [RelayService] runs in this process and shares the
 * application context, so it can read `RepositoryModule.repos` too.
 *
 * Phase 1 wiring is intentionally empty — the `Repositories` payload
 * holds no fields yet. Later phases add repositories here; a
 * regression that forgets to call `RepositoryModule.init` shows up
 * immediately as `lateinit` access on the first Activity launch.
 */
class SenseApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        RepositoryModule.init(this)
    }
}
