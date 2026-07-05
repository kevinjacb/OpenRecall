package com.sense.relay.data

import com.sense.relay.store.Config
import com.sense.relay.store.ServerConfig
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Test
import java.io.File

/**
 * ConfigurationRepository wraps [ServerConfig] (DataStore) and is the
 * only writer of app config. These tests pin:
 *   - `save(c)` round-trips every field.
 *   - `saveCredentials(url, token)` preserves device address and
 *     provisioned flag (the wizard writes only those two fields).
 *   - `observe()` emits the latest persisted value, not a stale one.
 */
class ConfigurationRepositoryTest {

    private fun repo(): Pair<ConfigurationRepositoryImpl, File> {
        val dir = File(System.getProperty("java.io.tmpdir"), "cfg_${System.nanoTime()}")
        return ConfigurationRepositoryImpl(ServerConfig(dir)) to dir
    }

    @Test fun saveRoundTripsAllFields() = runTest {
        val (repo, _) = repo()
        val c = Config(
            serverUrl = "wss://x:8766", token = "tok", deviceAddress = "AA:BB", provisioned = true,
        )
        repo.save(c)
        assertEquals(c, repo.observe().first())
    }

    @Test fun saveCredentialsPreservesDeviceAndProvisionedFlag() = runTest {
        val (repo, _) = repo()
        repo.save(Config(
            serverUrl = "wss://x:8766", token = "old", deviceAddress = "AA:BB", provisioned = true,
        ))
        repo.saveCredentials("wss://y:8766", "new")
        val observed = repo.observe().first()
        assertEquals("wss://y:8766", observed.serverUrl)
        assertEquals("new", observed.token)
        // The wizard's job is to save ONLY url+token. The address and
        // provisioned flag must be left as-is so re-entering the
        // wizard doesn't rewind provisioning.
        assertEquals("AA:BB", observed.deviceAddress)
        assertEquals(true, observed.provisioned)
    }

    @Test fun observeEmitsCurrentValueOnSubscribe() = runTest {
        val (repo, _) = repo()
        repo.save(Config(serverUrl = "wss://z:8766", token = "z"))
        val emitted = repo.observe().first()
        assertEquals("wss://z:8766", emitted.serverUrl)
        assertEquals("z", emitted.token)
    }

    @Test fun observeReactsToSubsequentSave() = runTest {
        val (repo, _) = repo()
        repo.save(Config(serverUrl = "wss://a:1", token = "a"))
        val first = repo.observe().first()
        repo.save(Config(serverUrl = "wss://a:2", token = "a"))
        val second = repo.observe().first()
        assertEquals("wss://a:1", first.serverUrl)
        assertEquals("wss://a:2", second.serverUrl)
    }
}
