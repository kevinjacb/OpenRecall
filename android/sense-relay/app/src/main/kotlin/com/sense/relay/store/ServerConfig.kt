package com.sense.relay.store

import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.PreferenceDataStoreFactory
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import java.io.File

data class Config(
    val serverUrl: String = "",
    val token: String = "",
    val deviceAddress: String? = null,
    val provisioned: Boolean = false,
)

private val KEY_URL = stringPreferencesKey("url")
private val KEY_TOKEN = stringPreferencesKey("token")
private val KEY_DEV = stringPreferencesKey("device")
private val KEY_PROV = booleanPreferencesKey("prov")

class ServerConfig(dir: File) {
    // Mirrors the path produced by androidx.datastore.preferences.PreferenceDataStoreFile's
    // Context.preferencesDataStoreFile(name): <dir>/datastore/<name>.preferences_pb
    private val store: DataStore<Preferences> =
        PreferenceDataStoreFactory.create(produceFile = {
            File(dir, "datastore/sense_config.preferences_pb")
        })

    suspend fun read(): Config = store.data.map { p ->
        Config(
            serverUrl = p[KEY_URL] ?: "",
            token = p[KEY_TOKEN] ?: "",
            deviceAddress = p[KEY_DEV],
            provisioned = p[KEY_PROV] ?: false,
        )
    }.first()

    suspend fun write(c: Config) {
        store.edit { p ->
            p[KEY_URL] = c.serverUrl
            p[KEY_TOKEN] = c.token
            c.deviceAddress?.let { p[KEY_DEV] = it } ?: run { p.remove(KEY_DEV) }
            p[KEY_PROV] = c.provisioned
        }
    }

    suspend fun clear() { store.edit { it.clear() } }
}