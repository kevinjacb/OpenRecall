package com.sense.relay.ui.settings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.sense.relay.data.ConfigurationRepository
import com.sense.relay.store.Config
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.stateIn

/**
 * Settings ViewModel. The only place that reads (and, in a future phase,
 * edits) the persisted [Config]. v1 is read-only display + a reconfigure
 * affordance that relaunches the wizard; `save` is kept for future in-place
 * edits (e.g. editing the URL without re-provisioning) and is covered by a
 * round-trip test.
 *
 * `state` is the [Config] directly — no sealed wrapper, because config is
 * always present (defaults to empty before provisioning) and the screen
 * renders the fields.
 */
class SettingsViewModel(
    private val config: ConfigurationRepository,
) : ViewModel() {

    val state: StateFlow<Config> = config.observe()
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), Config())

    /** Replace the persisted config wholesale (future in-place editing). */
    suspend fun save(c: Config) = config.save(c)
}