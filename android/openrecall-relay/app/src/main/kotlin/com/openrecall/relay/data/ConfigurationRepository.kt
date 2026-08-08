package com.openrecall.relay.data

import com.openrecall.relay.store.Config
import com.openrecall.relay.store.ServerConfig
import kotlinx.coroutines.flow.Flow

/**
 * The only writer of app config. Consumers (the setup wizard, the
 * Settings screen) read [observe] and call [save] / [saveCredentials];
 * nothing else touches [ServerConfig] directly. That single-writer
 * rule is what keeps persisted state internally consistent.
 */
interface ConfigurationRepository {
    /** Hot stream of the latest persisted [Config]. */
    fun observe(): Flow<Config>

    /** Replace the persisted config wholesale. */
    suspend fun save(c: Config)

    /**
     * Persist just URL + token. Preserves `deviceAddress` and
     * `provisioned` so re-entering the wizard doesn't rewind
     * provisioning. Used by the setup flow when the user is mid-edit.
     */
    suspend fun saveCredentials(url: String, token: String)
}

/**
 * Production impl. Delegates to the existing [ServerConfig] (the
 * DataStore wrapper); the wrapper is what gives us the
 * `Flow<Config>` (via [ServerConfig.observe]) and the
 * process-singleton DataStore (via [com.openrecall.relay.store.StoreHolder]).
 */
class ConfigurationRepositoryImpl(
    private val config: ServerConfig,
) : ConfigurationRepository {

    override fun observe(): Flow<Config> = config.observe()

    override suspend fun save(c: Config) = config.write(c)

    override suspend fun saveCredentials(url: String, token: String) =
        config.saveCredentials(url, token)
}
