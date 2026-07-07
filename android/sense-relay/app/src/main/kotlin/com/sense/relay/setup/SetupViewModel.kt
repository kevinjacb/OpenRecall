package com.sense.relay.setup
import com.sense.relay.store.Config
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

sealed interface SetupStep {
    data class EnterServer(val url: String = "", val token: String = "", val error: String? = null) : SetupStep
    data class Connecting(val msg: String) : SetupStep
    data class Scanning(val devices: List<String> = emptyList(), val error: String? = null) : SetupStep
    data class Provisioning(val msg: String) : SetupStep
    data class Done(val relayRunning: Boolean) : SetupStep
}

interface ServerApi {
    suspend fun health(urlBase: String, token: String): Boolean
    suspend fun pubkey(urlBase: String, token: String): ByteArray
    /**
     * The WS gateway port the server advertises on `/health`, or null if it
     * doesn't report one. The relay needs this to derive its WebSocket URL
     * (the HTTP API and the WS gateway are on separate ports). Fetch failures
     * return null so provisioning never blocks on this — the relay falls back
     * to the legacy same-port scheme-swap.
     */
    suspend fun gatewayPort(urlBase: String, token: String): Int?
}

interface DeviceScanner {
    suspend fun scan(): List<String>
    suspend fun connect(address: String): BleProvisioning
}

class SetupViewModel(
    private val serverApi: ServerApi,
    private val scanner: DeviceScanner,
    private val onAttempt: suspend (String, String) -> Unit = { _, _ -> },
    initialUrl: String = "",
    initialToken: String = "",
    private val onDone: suspend (Config) -> Unit,
) {
    private val _step = MutableStateFlow<SetupStep>(SetupStep.EnterServer(initialUrl, initialToken))
    val step: StateFlow<SetupStep> = _step.asStateFlow()

    suspend fun submitServer(url: String, token: String) {
        // Persist the entered credentials immediately so a failed attempt (or an app
        // restart mid-setup) pre-fills the form next time instead of forcing a retype.
        runCatching { onAttempt(url, token) }
        _step.value = SetupStep.Connecting("testing connection")
        try {
            if (!serverApi.health(url, token)) {
                _step.value = SetupStep.EnterServer(url, token, "server unreachable"); return
            }
            // Discover the WS gateway port (separate from the HTTP API port)
            // so the relay can build its WebSocket URL. Non-fatal: null falls
            // back to the legacy same-port scheme-swap in the relay.
            val gatewayPort = runCatching { serverApi.gatewayPort(url, token) }.getOrNull()
            val pubkey = serverApi.pubkey(url, token)
            _step.value = SetupStep.Scanning()
            val devices = scanner.scan()
            if (devices.isEmpty()) {
                _step.value = SetupStep.Scanning(emptyList(), "no Sense device found"); return
            }
            val ble = scanner.connect(devices.first())
            _step.value = SetupStep.Provisioning("provisioning device")
            ProvisioningClient(ble).ensureProvisioned(pubkey)
            onDone(Config(url, token, devices.first(), true, gatewayPort = gatewayPort))
            _step.value = SetupStep.Done(true)
        } catch (e: SecurityException) {
            _step.value = SetupStep.EnterServer(url, token, "bad token or unauthorized")
        } catch (e: Exception) {
            _step.value = SetupStep.EnterServer(url, token, e.message ?: "error")
        }
    }
}