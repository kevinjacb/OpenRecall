package com.openrecall.relay.domain.model

/**
 * Opaque session identifier. The wire format is a string (server-issued
 * ULID/UUID) but the rest of the app should never treat it as one — wrap
 * it here so a future move to a typed ID (or to a richer aggregate key)
 * is a one-file change.
 */
@JvmInline
value class SessionId(val value: String)
