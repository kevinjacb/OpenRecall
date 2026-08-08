package com.opensapien.relay.ui.settings

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.widget.Toast
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.opensapien.relay.core.ui.SenseTheme
import com.opensapien.relay.data.RepositoryModule
import com.opensapien.relay.store.Config
import com.opensapien.relay.ui.design.DangerButton
import com.opensapien.relay.ui.design.PlaceholderTag
import com.opensapien.relay.ui.design.SecondaryButton
import com.opensapien.relay.ui.design.SenseGroupLabel
import com.opensapien.relay.ui.design.SenseIcons
import com.opensapien.relay.ui.design.SenseScreenHeader
import com.opensapien.relay.ui.design.SettingsActionRow
import com.opensapien.relay.ui.design.SettingsGroup
import com.opensapien.relay.ui.design.SettingsToggleRow
import com.opensapien.relay.ui.design.SettingsValueRow

/**
 * Settings route. Builds the [SettingsViewModel] from the process singleton
 * and forwards the reconfigure action to [onReconfigure], which relaunches
 * the pairing wizard through MainActivity's activity-result launcher.
 *
 * [onOpenDevice] and [onOpenCommands] push the two diagnostic screens that
 * lost their bottom-bar slots in the redesign.
 */
@Composable
fun SettingsRoute(
    onReconfigure: () -> Unit,
    onOpenDevice: () -> Unit,
    onOpenCommands: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val vm: SettingsViewModel = viewModel(
        factory = viewModelFactory {
            initializer { SettingsViewModel(RepositoryModule.repos.configuration) }
        },
    )
    val config by vm.state.collectAsState()
    val capture by vm.capture.collectAsState()
    SettingsScreen(
        config = config,
        capture = capture,
        onCaptureChanged = vm::onCaptureChanged,
        onReconfigure = onReconfigure,
        onForgetDevice = vm::forgetDevice,
        onOpenDevice = onOpenDevice,
        onOpenCommands = onOpenCommands,
        modifier = modifier,
    )
}

/**
 * Stateless Settings content — the comp's grouped list: Device, Relay,
 * Capture toggles, the diagnostic drill-downs, then the two device actions.
 *
 * Value rows copy to the clipboard on tap, which is the only way to get a
 * token or an address off the phone.
 */
@Composable
fun SettingsScreen(
    config: Config,
    capture: CaptureSettings,
    onCaptureChanged: (CaptureSettings) -> Unit,
    onReconfigure: () -> Unit,
    onForgetDevice: () -> Unit,
    onOpenDevice: () -> Unit,
    onOpenCommands: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val colors = SenseTheme.colors
    val context = LocalContext.current
    var confirmForget by remember { mutableStateOf(false) }

    Column(
        modifier = modifier
            .fillMaxSize()
            .background(colors.canvas)
            .verticalScroll(rememberScrollState())
            .statusBarsPadding()
            .padding(horizontal = 20.dp)
            .padding(bottom = 28.dp),
    ) {
        SenseScreenHeader(eyebrow = "Settings", hero = "Your relay, your rules")

        SenseGroupLabel("Device", Modifier.padding(top = 26.dp, bottom = 12.dp))
        SettingsGroup {
            SettingsValueRow(
                label = "Address",
                value = config.deviceAddress ?: "Not paired",
                onClick = config.deviceAddress?.let {
                    { copy(context, "Address", it) }
                },
            )
            SettingsValueRow(
                label = "Status",
                value = if (config.provisioned) "Provisioned" else "Not set up",
                last = true,
            )
        }

        SenseGroupLabel("Relay", Modifier.padding(top = 26.dp, bottom = 12.dp))
        SettingsGroup {
            SettingsValueRow(
                label = "URL",
                value = config.serverUrl.ifBlank { "—" },
                onClick = { copy(context, "URL", config.serverUrl) },
            )
            SettingsValueRow(
                label = "Token",
                value = maskedToken(config.token),
                onClick = { copy(context, "Token", config.token) },
            )
            SettingsValueRow(
                label = "Gateway port",
                value = config.gatewayPort?.toString() ?: "Default",
                last = true,
            )
        }

        SenseGroupLabel("Capture", Modifier.padding(top = 26.dp, bottom = 12.dp))
        SettingsGroup {
            SettingsToggleRow(
                title = "Capture automatically",
                subtitle = "Start a session whenever the device hears speech.",
                checked = capture.autoCapture,
                onCheckedChange = { onCaptureChanged(capture.copy(autoCapture = it)) },
                trailing = { PlaceholderTag() },
            )
            SettingsToggleRow(
                title = "Wake word",
                subtitle = "Only listen after “Hey OpenSapien”.",
                checked = capture.wakeWord,
                onCheckedChange = { onCaptureChanged(capture.copy(wakeWord = it)) },
                trailing = { PlaceholderTag() },
            )
            SettingsToggleRow(
                title = "Redact names on device",
                subtitle = "Strip identifiers before audio leaves the phone.",
                checked = capture.redactNames,
                onCheckedChange = { onCaptureChanged(capture.copy(redactNames = it)) },
                last = true,
                trailing = { PlaceholderTag() },
            )
        }
        Text(
            text = "These preferences are saved on this phone. The firmware and relay " +
                "don't expose controls for them yet, so they don't change capture " +
                "behaviour today.",
            style = MaterialTheme.typography.bodySmall,
            color = colors.grey,
            modifier = Modifier.padding(top = 10.dp),
        )

        SenseGroupLabel("Diagnostics", Modifier.padding(top = 26.dp, bottom = 12.dp))
        SettingsGroup {
            SettingsActionRow(
                title = "Device",
                subtitle = "BLE link, relay lifecycle, server status",
                onClick = onOpenDevice,
                trailing = { ChevronIcon() },
            )
            SettingsActionRow(
                title = "Commands",
                subtitle = "Active and recent agent requests",
                onClick = onOpenCommands,
                last = true,
                trailing = { ChevronIcon() },
            )
        }

        SecondaryButton(
            label = "Reconfigure device",
            onClick = onReconfigure,
            modifier = Modifier.padding(top = 26.dp),
        )
        DangerButton(
            label = "Forget this device",
            onClick = { confirmForget = true },
            modifier = Modifier.padding(top = 6.dp),
        )

        Text(
            text = "OpenSapien Relay 0.1 · ${config.serverUrl.ifBlank { "no relay" }}",
            style = MaterialTheme.typography.bodySmall,
            color = colors.greyFaint,
            textAlign = TextAlign.Center,
            modifier = Modifier.padding(top = 16.dp).align(androidx.compose.ui.Alignment.CenterHorizontally),
        )
    }

    if (confirmForget) {
        AlertDialog(
            onDismissRequest = { confirmForget = false },
            containerColor = colors.card,
            titleContentColor = colors.ink,
            textContentColor = colors.inkMuted,
            title = { Text("Forget this device?") },
            text = {
                Text(
                    "The relay URL, token and paired device are erased from this phone. " +
                        "Recordings already on the server are untouched. You'll need to " +
                        "run setup again to reconnect.",
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    confirmForget = false
                    onForgetDevice()
                }) { Text("Forget", color = colors.danger) }
            },
            dismissButton = {
                TextButton(onClick = { confirmForget = false }) {
                    Text("Cancel", color = colors.slate)
                }
            },
        )
    }
}

@Composable
private fun ChevronIcon() {
    Icon(
        SenseIcons.ChevronRight,
        contentDescription = null,
        tint = SenseTheme.colors.greyFaint,
        modifier = Modifier.size(18.dp),
    )
}

/** Mask the token for display — first and last three characters, middle hidden. */
private fun maskedToken(token: String): String = when {
    token.isBlank() -> "—"
    token.length <= 6 -> "•".repeat(token.length)
    else -> "${token.take(3)}${"•".repeat(6)}${token.takeLast(3)}"
}

/** Copy [value] to the system clipboard and toast a confirmation. */
private fun copy(context: Context, label: String, value: String) {
    if (value.isBlank()) return
    val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
    clipboard.setPrimaryClip(ClipData.newPlainText(label, value))
    Toast.makeText(context, "$label copied", Toast.LENGTH_SHORT).show()
}
