package com.sense.relay.ui.nav

import android.os.Bundle
import androidx.navigation.NavType
import com.sense.relay.domain.model.SessionId

/**
 * Nav Compose [NavType] for the [SessionId] value class. Backed by
 * [NavType.StringType] — the wire format is a string — and adds the
 * minimal validation we need at the nav layer (reject empty strings
 * that would otherwise surface as a real `SessionId("")` downstream).
 *
 * Used by the typed `composable<Destination.SessionDetail>` call:
 * ```
 * composable<Destination.SessionDetail>(
 *     typeMap = mapOf(typeOf<SessionId>() to SessionIdNavType),
 * ) { ... }
 * ```
 *
 * Nav Compose 2.8's `parseValue` is contractually non-nullable; invalid
 * input throws [IllegalArgumentException] so the framework can fail-soft
 * at the deep-link boundary.
 */
object SessionIdNavType : NavType<SessionId>(isNullableAllowed = false) {

    override fun get(bundle: Bundle, key: String): SessionId? {
        // StringType's convention: the value is stored under the same key.
        // An absent key means the arg wasn't provided; surface that as null
        // so the caller can show a missing-arg error instead of crashing.
        val s = bundle.getString(key) ?: return null
        if (s.isEmpty()) return null
        return SessionId(s)
    }

    override fun parseValue(value: String): SessionId {
        // Reject the empty string explicitly: it isn't a valid SessionId and
        // silently coercing it would let a malformed deep-link land on a
        // page that can't load. Throwing matches the NavType contract
        // (IllegalArgumentException is what the framework handles).
        require(value.isNotEmpty()) { "SessionId cannot be empty" }
        return SessionId(value)
    }

    override fun put(bundle: Bundle, key: String, value: SessionId) {
        bundle.putString(key, value.value)
    }

    override val name: String = "session_id"
}
