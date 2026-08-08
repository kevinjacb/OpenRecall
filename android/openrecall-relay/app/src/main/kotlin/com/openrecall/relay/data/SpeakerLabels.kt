package com.openrecall.relay.data

/**
 * Assigns "Person N" placeholder labels to unnamed non-wearer speakers, in
 * the given (first-appearance) order. Named speakers and the wearer keep
 * their real name / "You" — the caller renders those; this only fills the
 * gaps. Stable for a given input ordering; pure (no state), cheap to
 * recompute per render pass.
 *
 * The server keeps ``display_name=null`` for unnamed speakers; this label is
 * a client-only affordance so the user never sees a bare "null" or "?".
 */
fun personLabels(speakersInOrder: List<SpeakerEntry>): Map<String, String> {
    var n = 0
    return speakersInOrder
        .filter { !it.isWearer && it.name == null }
        .associate { it.speakerId to "Person ${++n}" }
}
