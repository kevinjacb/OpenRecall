package com.sense.relay.store

import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.PreferenceDataStoreFactory
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import java.io.File

data class Config(
    val serverUrl: String = "",
    val token: String = "",
    val deviceAddress: String? = null,
    val provisioned: Boolean = false,
    /**
     * The WS gateway port the server advertised on `/health` at provisioning
     * time. The HTTP control API (the URL in [serverUrl]) and the WS gateway
     * run on separate ports, so the relay needs this to derive its WebSocket
     * URL. Null when the server didn't report one (older server / no gateway),
     * in which case the relay falls back to the legacy same-port scheme-swap.
     */
    val gatewayPort: Int? = null,
)

private val KEY_URL = stringPreferencesKey("url")
private val KEY_TOKEN = stringPreferencesKey("token")
private val KEY_DEV = stringPreferencesKey("device")
private val KEY_PROV = booleanPreferencesKey("prov")
private val KEY_GATEWAY_PORT = intPreferencesKey("gateway_port")

/**
 * Persisted server config (URL, token, device address, provisioned flag).
 *
 * DataStore is a process-wide singleton per file — creating more than one
 * DataStore pointing at the same `.preferences_pb` throws
 * `IllegalStateException: There are multiple DataStores active for the same file`.
 * This class is constructed three times per session (SetupActivity.onCreate's
 * prefilled-form read, SetupActivity.onDone's write after provisioning,
 * RelayService.onStartCommand's read on start, and again on every
 * SetupViewModel.onAttempt), so we MUST share a single DataStore instance.
 *
 * `StoreHolder` keys the singleton by the absolute file path so the same
 * ServerConfig(dir) call (from any Activity, Service, or coroutine) returns
 * a wrapper around the same underlying DataStore. DataStore's own internal
 * single-writer queue handles concurrent read/write from different call sites.
 */
class ServerConfig(dir: File) {

    private val store: DataStore<Preferences> = StoreHolder.get(File(dir, DATASTORE_FILE))

    suspend fun read(): Config = store.data.map { p ->
        Config(
            serverUrl = p[KEY_URL] ?: "",
            token = p[KEY_TOKEN] ?: "",
            deviceAddress = p[KEY_DEV],
            provisioned = p[KEY_PROV] ?: false,
            gatewayPort = p[KEY_GATEWAY_PORT],
        )
    }.first()

    /**
     * Hot stream of the persisted [Config]. Emits the current value on
     * subscribe and a fresh value on every write. Phase 2 introduces
     * this so [com.sense.relay.data.ConfigurationRepository.observe]
     * can be a thin wrapper; later phases (Settings auto-save, wizard
     * re-entry) collect from this same flow instead of polling.
     */
    fun observe(): Flow<Config> = store.data.map { p ->
        Config(
            serverUrl = p[KEY_URL] ?: "",
            token = p[KEY_TOKEN] ?: "",
            deviceAddress = p[KEY_DEV],
            provisioned = p[KEY_PROV] ?: false,
            gatewayPort = p[KEY_GATEWAY_PORT],
        )
    }

    suspend fun write(c: Config) {
        store.edit { p ->
            p[KEY_URL] = c.serverUrl
            p[KEY_TOKEN] = c.token
            c.deviceAddress?.let { p[KEY_DEV] = it } ?: run { p.remove(KEY_DEV) }
            p[KEY_PROV] = c.provisioned
            c.gatewayPort?.let { p[KEY_GATEWAY_PORT] = it } ?: run { p.remove(KEY_GATEWAY_PORT) }
        }
    }

    /** Persist just the server URL + token entered in the setup form, so a failed attempt
     *  (or app restart) pre-fills the form next time. Preserves device address + provisioned. */
    suspend fun saveCredentials(url: String, token: String) {
        store.edit { p ->
            p[KEY_URL] = url
            p[KEY_TOKEN] = token
        }
    }

    suspend fun clear() { store.edit { it.clear() } }

    companion object {
        // Mirrors the path produced by androidx.datastore.preferences.PreferenceDataStoreFile's
        // Context.preferencesDataStoreFile(name): <dir>/datastore/<name>.preferences_pb
        private const val DATASTORE_FILE = "datastore/sense_config.preferences_pb"
    }
}

/** Process-wide map of absolute file path → DataStore. Keying by path means the
 *  same ServerConfig(dir) call (from SetupActivity, RelayService, etc.) gets the
 *  same DataStore instance, which is what DataStore requires. */
private object StoreHolder {
    private val stores = mutableMapOf<String, DataStore<Preferences>>()

    @Synchronized
    fun get(file: File): DataStore<Preferences> =
        stores.getOrPut(file.absolutePath) {
            PreferenceDataStoreFactory.create(produceFile = { file })
        }
}
