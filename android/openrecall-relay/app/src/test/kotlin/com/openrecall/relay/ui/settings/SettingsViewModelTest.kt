package com.openrecall.relay.ui.settings

import com.openrecall.relay.data.ConfigurationRepository
import com.openrecall.relay.store.Config
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import kotlin.test.AfterTest
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals

/**
 * Pins [SettingsViewModel]: it surfaces the persisted [Config] and round-trips
 * a [save]. The fake [ConfigurationRepository] is a [MutableStateFlow]; the
 * test holds a collecting subscriber in `backgroundScope` so
 * `stateIn(WhileSubscribed)` collects the upstream, then reads `vm.state.value`
 * (the Phase-6 pattern).
 */
@OptIn(ExperimentalCoroutinesApi::class)
class SettingsViewModelTest {

    private val dispatcher = StandardTestDispatcher()

    @BeforeTest fun setUp() = Dispatchers.setMain(dispatcher)
    @AfterTest fun tearDown() = Dispatchers.resetMain()

    private class FakeConfigurationRepository(initial: Config) : ConfigurationRepository {
        val flow = MutableStateFlow(initial)
        var lastSaved: Config? = null
        override fun observe(): Flow<Config> = flow
        override suspend fun save(c: Config) { lastSaved = c }
        override suspend fun saveCredentials(url: String, token: String) {
            flow.value = flow.value.copy(serverUrl = url, token = token)
        }
    }

    private fun subscribe(scope: CoroutineScope, vm: SettingsViewModel) {
        scope.launch { vm.state.toList(mutableListOf()) }
    }

    @Test fun initialStateIsThePersistedConfig() = runTest(dispatcher) {
        val config = Config(serverUrl = "https://s:8766", token = "tok", deviceAddress = "AA", provisioned = true)
        val vm = SettingsViewModel(FakeConfigurationRepository(config))
        subscribe(backgroundScope, vm)
        testScheduler.advanceUntilIdle()
        assertEquals(config, vm.state.value)
    }

    @Test fun configChangeReEmits() = runTest(dispatcher) {
        val repo = FakeConfigurationRepository(Config())
        val vm = SettingsViewModel(repo)
        subscribe(backgroundScope, vm)
        testScheduler.advanceUntilIdle()
        assertEquals(Config(), vm.state.value)

        repo.flow.value = Config(serverUrl = "https://new:8766", token = "t2", provisioned = true)
        testScheduler.advanceUntilIdle()
        assertEquals("https://new:8766", vm.state.value.serverUrl)
        assertEquals("t2", vm.state.value.token)
    }

    @Test fun saveRoundTripsToTheRepository() = runTest(dispatcher) {
        val repo = FakeConfigurationRepository(Config())
        val vm = SettingsViewModel(repo)
        subscribe(backgroundScope, vm)
        val toSave = Config(serverUrl = "https://saved:8766", token = "tk", provisioned = true)
        vm.save(toSave)
        assertEquals(toSave, repo.lastSaved, "save delegates to the repository")
    }

    @Test fun forgetDeviceClearsEveryCredential() = runTest(dispatcher) {
        // "Forget this Sense" must leave nothing behind: a surviving token or
        // provisioned flag would keep Home claiming a device is paired and
        // keep the relay retrying against a server the user disowned.
        val repo = FakeConfigurationRepository(
            Config(
                serverUrl = "https://s:8766",
                token = "tok",
                deviceAddress = "AA:BB",
                provisioned = true,
                gatewayPort = 8765,
            ),
        )
        val vm = SettingsViewModel(repo)
        subscribe(backgroundScope, vm)

        vm.forgetDevice()
        testScheduler.advanceUntilIdle()

        assertEquals(Config(), repo.lastSaved, "an empty config is persisted")
    }

    @Test fun captureTogglesStartAtTheDesignDefaults() = runTest(dispatcher) {
        val vm = SettingsViewModel(FakeConfigurationRepository(Config()))
        assertEquals(CaptureSettings(), vm.capture.value)

        vm.onCaptureChanged(CaptureSettings(wakeWord = true))
        assertEquals(true, vm.capture.value.wakeWord)
    }
}