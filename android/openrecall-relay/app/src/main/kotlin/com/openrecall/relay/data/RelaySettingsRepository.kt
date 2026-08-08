package com.openrecall.relay.data

import com.openrecall.relay.core.result.Outcome
import com.openrecall.relay.domain.model.DeviceStatus
import com.openrecall.relay.domain.model.RelaySettings
import com.openrecall.relay.http.OpenRecallHttpClient
import com.openrecall.relay.http.dto.toDomain
import com.openrecall.relay.http.dto.toDto
import kotlinx.coroutines.CancellationException

/**
 * The relay's durable settings, and what it knows about the device.
 *
 * Settings live on the **server**, not the phone. That is deliberate: a
 * command is delivered only to a live gateway session, so a phone-local
 * toggle would be dead whenever the wearable is offline, which is most of
 * the time. The server persists the desired value and reconciles the device
 * against it on every reconnect — so flipping a switch here means something
 * even with the device in a drawer.
 */
interface RelaySettingsRepository {
    suspend fun load(): Outcome<RelaySettings>

    /**
     * Persist the whole document. The server merges and returns the result,
     * which is what the caller should render — a `PUT` that changes
     * `audioEnabled` also kicks reconciliation, so the returned document is
     * the authoritative one.
     */
    suspend fun save(settings: RelaySettings): Outcome<RelaySettings>

    suspend fun deviceStatus(): Outcome<DeviceStatus>
}

/** Production impl over the shared config-aware client provider. */
class HttpRelaySettingsRepository(
    private val client: suspend () -> OpenRecallHttpClient,
) : RelaySettingsRepository {

    override suspend fun load(): Outcome<RelaySettings> =
        attempt { client().getSettings().toDomain() }

    override suspend fun save(settings: RelaySettings): Outcome<RelaySettings> =
        attempt { client().putSettings(settings.toDto()).toDomain() }

    override suspend fun deviceStatus(): Outcome<DeviceStatus> =
        attempt { client().getDeviceStatus().toDomain() }

    private suspend fun <T> attempt(block: suspend () -> T): Outcome<T> = try {
        Outcome.Success(block())
    } catch (e: CancellationException) {
        throw e
    } catch (e: Throwable) {
        Outcome.Failure(httpApiError(e))
    }
}
