package com.openrecall.relay.ui.settings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.openrecall.relay.data.ConfigurationRepository
import com.openrecall.relay.store.Config
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

/**
 * The three capture toggles from the `design/` comp.
 *
 * **Placeholder.** Nothing consumes these yet: the firmware exposes no
 * always-on/wake-word mode over its provisioning service, and the relay has
 * no on-device redaction stage. They are held in memory so the Settings
 * screen is interactive and the wiring is a one-line change once those
 * capabilities exist — see the note the screen renders beneath them.
 */
data class CaptureSettings(
    val autoCapture: Boolean = true,
    val wakeWord: Boolean = false,
    val redactNames: Boolean = true,
)

/**
 * Settings ViewModel. The only place that reads and clears the persisted
 * [Config].
 *
 * `state` is the [Config] directly — no sealed wrapper, because config is
 * always present (it defaults to empty before provisioning) and the screen
 * renders whatever fields it has.
 */
class SettingsViewModel(
    private val config: ConfigurationRepository,
) : ViewModel() {

    val state: StateFlow<Config> = config.observe()
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), Config())

    private val _capture = MutableStateFlow(CaptureSettings())
    val capture: StateFlow<CaptureSettings> = _capture.asStateFlow()

    fun onCaptureChanged(value: CaptureSettings) {
        _capture.value = value
    }

    /** Replace the persisted config wholesale (future in-place editing). */
    suspend fun save(c: Config) = config.save(c)

    /**
     * "Forget this device": erase the relay URL, token, paired address and
     * the provisioned flag. Writing an empty [Config] rather than deleting
     * the store keeps every observer on a valid value — Home flips straight
     * back to its "Not connected / set up" state.
     */
    fun forgetDevice() {
        viewModelScope.launch { config.save(Config()) }
    }
}
