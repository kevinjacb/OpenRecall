package com.sense.relay.core.model

/**
 * A clock abstraction so production code reads `clock.nowMs()` and tests can
 * pass a fixed clock. The default is the JVM's wall clock.
 */
interface Clock {
    fun nowMs(): Long
}

object SystemClock : Clock {
    override fun nowMs(): Long = java.lang.System.currentTimeMillis()
}
