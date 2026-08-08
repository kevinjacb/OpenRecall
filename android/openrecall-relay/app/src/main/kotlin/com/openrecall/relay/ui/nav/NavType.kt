package com.openrecall.relay.ui.nav

import android.os.Bundle
import androidx.navigation.NavType
import com.openrecall.relay.domain.model.SegmentId

/**
 * Nav Compose [NavType] for the [SegmentId] value class. Backed by
 * [NavType.StringType] — the wire format is a string — and adds the minimal
 * validation we need at the nav layer (reject empty strings that would
 * otherwise surface as a real `SegmentId("")` downstream).
 *
 * Ids look like `"<session_id>:<seq>"`. The route builder percent-encodes
 * them and the framework decodes before [parseValue] is called, so this type
 * always sees the raw id.
 *
 * Nav Compose 2.8's `parseValue` is contractually non-nullable; invalid input
 * throws [IllegalArgumentException] so the framework can fail-soft at the
 * deep-link boundary.
 */
object SegmentIdNavType : NavType<SegmentId>(isNullableAllowed = false) {

    override fun get(bundle: Bundle, key: String): SegmentId? {
        // StringType's convention: the value is stored under the same key.
        // An absent key means the arg wasn't provided; surface that as null
        // so the caller can show a missing-arg error instead of crashing.
        val s = bundle.getString(key) ?: return null
        if (s.isEmpty()) return null
        return SegmentId(s)
    }

    override fun parseValue(value: String): SegmentId {
        // Reject the empty string explicitly: it isn't a valid SegmentId and
        // silently coercing it would let a malformed deep-link land on a page
        // that can't load. Throwing matches the NavType contract
        // (IllegalArgumentException is what the framework handles).
        require(value.isNotEmpty()) { "SegmentId cannot be empty" }
        return SegmentId(value)
    }

    override fun put(bundle: Bundle, key: String, value: SegmentId) {
        bundle.putString(key, value.value)
    }

    override val name: String = "segment_id"
}
