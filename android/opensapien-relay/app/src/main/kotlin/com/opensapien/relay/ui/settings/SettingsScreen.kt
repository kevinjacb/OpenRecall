package com.opensapien.relay.ui.settings

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.widget.Toast
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import androidx.compose.runtime.collectAsState
import com.opensapien.relay.core.ui.Spacing
import com.opensapien.relay.data.RepositoryModule
import com.opensapien.relay.store.Config
import com.opensapien.relay.ui.design.InfoRow
import com.opensapien.relay.ui.design.SectionHeader

/**
 * Settings route. Builds the [SettingsViewModel] from the process singleton
 * and forwards the reconfigure action to [onReconfigure] (relaunches the
 * wizard from MainActivity's activity-result launcher).
 */
@Composable
fun SettingsRoute(onReconfigure: () -> Unit, modifier: Modifier = Modifier) {
    val vm: SettingsViewModel = viewModel(
        factory = viewModelFactory {
            initializer { SettingsViewModel(RepositoryModule.repos.configuration) }
        },
    )
    val config by vm.state.collectAsState()
    SettingsScreen(config = config, onReconfigure = onReconfigure, modifier = modifier)
}

/**
 * Stateless Settings content. Read-only config display (server URL, token,
 * device address) — tap a row to copy it. The "Reconfigure device" button
 * relaunches the wizard (the only edit path in v1).
 */
@Composable
fun SettingsScreen(
    config: Config,
    onReconfigure: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val context = androidx.compose.ui.platform.LocalContext.current
    Column(
        modifier = modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(Spacing.md),
        verticalArrangement = Arrangement.spacedBy(Spacing.md),
    ) {
        SectionHeader(title = "Server")
        InfoRow(
            label = "URL",
            value = config.serverUrl.ifBlank { "—" },
            modifier = Modifier.fillMaxWidth().clickable { copy(context, "URL", config.serverUrl) },
        )
        InfoRow(
            label = "Token",
            value = maskedToken(config.token),
            modifier = Modifier.fillMaxWidth().clickable { copy(context, "Token", config.token) },
        )

        SectionHeader(title = "Device")
        InfoRow(label = "Address", value = config.deviceAddress ?: "—")

        Button(onClick = onReconfigure, modifier = Modifier.fillMaxWidth()) {
            Text("Reconfigure device")
        }

        if (!config.provisioned) {
            Text(
                "Not provisioned yet — tap Reconfigure to set up your OpenSapien device.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

/** Mask the token for display (show first/last 3 chars, hide the middle). */
private fun maskedToken(token: String): String = when {
    token.isBlank() -> "—"
    token.length <= 6 -> "•".repeat(token.length)
    else -> "${token.take(3)}${"•".repeat(token.length - 6)}${token.takeLast(3)}"
}

/** Copy [value] to the system clipboard and toast a confirmation. */
private fun copy(context: Context, label: String, value: String) {
    if (value.isBlank()) return
    val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
    clipboard.setPrimaryClip(ClipData.newPlainText(label, value))
    Toast.makeText(context, "$label copied", Toast.LENGTH_SHORT).show()
}