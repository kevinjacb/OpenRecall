package com.openrecall.relay.ui.settings

import com.openrecall.relay.core.model.ApiError
import com.openrecall.relay.core.result.Outcome
import com.openrecall.relay.data.ConfigurationRepository
import com.openrecall.relay.data.RelaySettingsRepository
import com.openrecall.relay.domain.model.CaptureSettings
import com.openrecall.relay.domain.model.DeviceStatus
import com.openrecall.relay.domain.model.RelaySettings
import com.openrecall.relay.domain.model.RetentionSettings
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
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * Pins [SettingsViewModel]: it surfaces the persisted [Config], round-trips a
 * [SettingsViewModel.save], and drives the relay's capture settings.
 *
 * The capture toggles are the interesting part. They are server-side desired
 * state, not phone preferences, so the tests assert the write actually
 * reaches the relay and that a failed write leaves the switch where the
 * server still has it — a switch that silently keeps a value the relay
 * rejected is the failure mode worth pinning.
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

    private class FakeRelaySettingsRepository(
        var loaded: Outcome<RelaySettings> = Outcome.Success(RelaySettings()),
        var status: Outcome<DeviceStatus> = Outcome.Failure(ApiError.Http(404)),
    ) : RelaySettingsRepository {
        var saved: RelaySettings? = null
        /** When set, a save fails with this instead of echoing the document. */
        var saveFailure: ApiError? = null

        override suspend fun load(): Outcome<RelaySettings> = loaded

        override suspend fun save(settings: RelaySettings): Outcome<RelaySettings> {
            saved = settings
            saveFailure?.let { return Outcome.Failure(it) }
            loaded = Outcome.Success(settings)
            return Outcome.Success(settings)
        }

        override suspend fun deviceStatus(): Outcome<DeviceStatus> = status
    }

    private fun subscribe(scope: CoroutineScope, vm: SettingsViewModel) {
        scope.launch { vm.state.toList(mutableListOf()) }
    }

    private fun viewModel(
        config: ConfigurationRepository,
        relay: RelaySettingsRepository = FakeRelaySettingsRepository(),
    ) = SettingsViewModel(config, relay)

    @Test fun initialStateIsThePersistedConfig() = runTest(dispatcher) {
        val config = Config(
            serverUrl = "https://s:8766", token = "tok", deviceAddress = "AA", provisioned = true,
        )
        val vm = viewModel(FakeConfigurationRepository(config))
        subscribe(backgroundScope, vm)
        testScheduler.advanceUntilIdle()
        assertEquals(config, vm.state.value)
    }

    @Test fun configChangeReEmits() = runTest(dispatcher) {
        val repo = FakeConfigurationRepository(Config())
        val vm = viewModel(repo)
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
        val vm = viewModel(repo)
        subscribe(backgroundScope, vm)
        val toSave = Config(serverUrl = "https://saved:8766", token = "tk", provisioned = true)
        vm.save(toSave)
        assertEquals(toSave, repo.lastSaved, "save delegates to the repository")
    }

    @Test fun forgetDeviceClearsEveryCredential() = runTest(dispatcher) {
        // "Forget this device" must leave nothing behind: a surviving token or
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
        val vm = viewModel(repo)
        subscribe(backgroundScope, vm)

        vm.forgetDevice()
        testScheduler.advanceUntilIdle()

        assertEquals(Config(), repo.lastSaved, "an empty config is persisted")
    }

    @Test fun captureTogglesAreInertUntilTheRelayAnswers() = runTest(dispatcher) {
        // Rendering defaults before the server has spoken would show switches
        // in positions the relay may not agree with.
        val relay = FakeRelaySettingsRepository(
            loaded = Outcome.Failure(ApiError.Unreachable("down")),
        )
        val vm = viewModel(FakeConfigurationRepository(Config()), relay)
        testScheduler.advanceUntilIdle()

        assertNull(vm.capture.value.settings, "no document, no switches")
        assertNotNull(vm.capture.value.error)

        vm.onCaptureChanged(CaptureSettings(audioEnabled = false))
        testScheduler.advanceUntilIdle()
        assertNull(relay.saved, "a toggle with no loaded document writes nothing")
    }

    @Test fun captureChangeIsWrittenThroughToTheRelay() = runTest(dispatcher) {
        val relay = FakeRelaySettingsRepository(
            loaded = Outcome.Success(
                RelaySettings(CaptureSettings(), RetentionSettings(audioDays = 30)),
            ),
        )
        val vm = viewModel(FakeConfigurationRepository(Config()), relay)
        testScheduler.advanceUntilIdle()

        vm.onCaptureChanged(CaptureSettings(audioEnabled = false))
        testScheduler.advanceUntilIdle()

        assertEquals(false, relay.saved?.capture?.audioEnabled, "reached the relay")
        assertEquals(
            30,
            relay.saved?.retention?.audioDays,
            "the whole document is sent, so retention isn't reset by a capture change",
        )
        assertEquals(false, vm.capture.value.settings?.capture?.audioEnabled)
        assertNull(vm.capture.value.error)
    }

    @Test fun failedCaptureWriteRevertsTheToggleAndSaysSo() = runTest(dispatcher) {
        // Leaving the switch in the position the user tapped would tell them
        // capture is off when the relay is still recording.
        val relay = FakeRelaySettingsRepository(
            loaded = Outcome.Success(RelaySettings(CaptureSettings(audioEnabled = true))),
        )
        relay.saveFailure = ApiError.Unreachable("down")
        val vm = viewModel(FakeConfigurationRepository(Config()), relay)
        testScheduler.advanceUntilIdle()

        vm.onCaptureChanged(CaptureSettings(audioEnabled = false))
        testScheduler.advanceUntilIdle()

        assertEquals(
            true,
            vm.capture.value.settings?.capture?.audioEnabled,
            "the switch is back where the relay still has it",
        )
        assertNotNull(vm.capture.value.error)
    }

    @Test fun deviceStatusIsSurfacedWhenTheRelayHasIt() = runTest(dispatcher) {
        val status = DeviceStatus(
            measured = false,
            batteryPct = 0.45,
            storageFreeBytes = null,
            firmwareVersion = null,
            recording = true,
            relayConnected = true,
            microphoneAvailable = true,
            cameraAvailable = false,
            lastPacketAt = null,
            lastPacketAgeS = 4.0,
            lastTranscriptAt = null,
            lastTranscriptAgeS = 120.0,
        )
        val relay = FakeRelaySettingsRepository(status = Outcome.Success(status))
        val vm = viewModel(FakeConfigurationRepository(Config()), relay)
        testScheduler.advanceUntilIdle()

        val device = assertNotNull(vm.device.value)
        assertTrue(device.isFresh(), "a 4s-old packet is a live link")
        assertEquals(false, device.measured, "the battery figure is a placeholder, and says so")
    }

    @Test fun deviceStatusFailureLeavesItNullRatherThanGuessing() = runTest(dispatcher) {
        val relay = FakeRelaySettingsRepository(status = Outcome.Failure(ApiError.Unreachable("x")))
        val vm = viewModel(FakeConfigurationRepository(Config()), relay)
        testScheduler.advanceUntilIdle()
        assertNull(vm.device.value)
    }
}
