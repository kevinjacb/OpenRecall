package com.openrecall.relay

import android.app.Application
import com.openrecall.relay.data.RepositoryModule

/**
 * Application entry point. Constructs the [RepositoryModule] singleton
 * here so it's ready before any Activity (or the relay service) reads
 * it. The [RelayService] runs in this process and shares the
 * application context, so it can read `RepositoryModule.repos` too.
 *
 * The wiring is idempotent — a re-init is a no-op so an
 * instrumentation test that re-attaches the application doesn't
 * double-construct.
 */
class RecallApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        RepositoryModule.init(this)
    }
}
