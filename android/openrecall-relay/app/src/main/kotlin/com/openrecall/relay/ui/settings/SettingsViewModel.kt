package com.openrecall.relay.ui.settings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.openrecall.relay.core.result.Outcome
import com.openrecall.relay.core.ui.toDisplayMessage
import com.openrecall.relay.data.ConfigurationRepository
import com.openrecall.relay.data.RelaySettingsRepository
import com.openrecall.relay.domain.model.CaptureSettings
import com.openrecall.relay.domain.model.DeviceStatus
import com.openrecall.relay.domain.model.RelaySettings
import com.openrecall.relay.store.Config
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * The capture/retention section's state.
 *
 * [settings] is null until the first load lands; the toggles render disabled
 * until then rather than showing defaults the server may not agree with — a
 * switch that reads "on" before anyone has asked the server is a lie the user
 * would act on.
 */
data class CaptureUiState(
    val settings: RelaySettings? = null,
    val loading: Boolean = true,
    /** Non-null when loading or saving failed. */
    val error: String? = null,
    /** True while a write is in flight; the toggles stay interactive but the
     *  section shows it is syncing. */
    val saving: Boolean = false,
)

/**
 * Settings ViewModel.
 *
 * Owns three separate things:
 *
 * * The persisted phone-local [Config] (relay URL, token, paired address).
 * * The relay's **server-side** capture and retention settings. These are not
 *   phone preferences: a command reaches the wearable only while it is
 *   connected, so the server holds the desired state and reconciles the
 *   device against it on reconnect. A toggle here therefore keeps meaning
 *   with the device in a drawer.
 * * The device status behind the header.
 */
class SettingsViewModel(
    private val config: ConfigurationRepository,
    private val relaySettings: RelaySettingsRepository,
) : ViewModel() {

    val state: StateFlow<Config> = config.observe()
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), Config())

    private val _capture = MutableStateFlow(CaptureUiState())
    val capture: StateFlow<CaptureUiState> = _capture.asStateFlow()

    private val _device = MutableStateFlow<DeviceStatus?>(null)
    /** Null until the first fetch lands, or if it fails — the header falls
     *  back to the relay-connection state it already has. */
    val device: StateFlow<DeviceStatus?> = _device.asStateFlow()

    init {
        refresh()
    }

    /** Re-read both the settings document and the device status. */
    fun refresh() {
        viewModelScope.launch {
            _capture.update { it.copy(loading = true) }
            when (val r = relaySettings.load()) {
                is Outcome.Success -> _capture.value = CaptureUiState(
                    settings = r.value, loading = false, error = null,
                )
                is Outcome.Failure -> _capture.update {
                    it.copy(loading = false, error = r.error.toDisplayMessage())
                }
            }
        }
        viewModelScope.launch {
            _device.value = (relaySettings.deviceStatus() as? Outcome.Success)?.value
        }
    }

    /**
     * Write a capture change through to the relay.
     *
     * Optimistic: the switch moves immediately and reverts if the write
     * fails, because a toggle that visibly lags a round trip reads as broken
     * and gets tapped twice. The server returns the merged document, which is
     * what we settle on — it is authoritative, and a `PUT` that changes the
     * microphone gate also kicks device reconciliation.
     */
    fun onCaptureChanged(value: CaptureSettings) {
        val current = _capture.value.settings ?: return
        val optimistic = current.copy(capture = value)
        _capture.value = _capture.value.copy(settings = optimistic, saving = true, error = null)
        viewModelScope.launch {
            when (val r = relaySettings.save(optimistic)) {
                is Outcome.Success -> _capture.value = CaptureUiState(
                    settings = r.value, loading = false, error = null, saving = false,
                )
                is Outcome.Failure -> _capture.value = CaptureUiState(
                    settings = current,
                    loading = false,
                    saving = false,
                    error = "Couldn't save to the relay: ${r.error.toDisplayMessage()}",
                )
            }
            // Changing the microphone gate makes the server issue a command,
            // so the device's reported state is stale the moment we return.
            _device.value = (relaySettings.deviceStatus() as? Outcome.Success)?.value
        }
    }

    /** Replace the persisted config wholesale (future in-place editing). */
    suspend fun save(c: Config) = config.save(c)

    /**
     * "Forget this device": erase the relay URL, token, paired address and
     * the provisioned flag. Writing an empty [Config] rather than deleting
     * the store keeps every observer on a valid value — Home flips straight
     * back to its "Not connected / set up" state.
     *
     * Local only. Recordings on the relay are untouched: a server-side purge
     * is a distinct and far more dangerous operation, and the server
     * deliberately ships no endpoint for it.
     */
    fun forgetDevice() {
        viewModelScope.launch { config.save(Config()) }
    }
}