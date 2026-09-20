package com.openrecall.relay.ui.settings

import android.content.Context
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
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
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.openrecall.relay.core.ui.RecallTheme
import com.openrecall.relay.data.RepositoryModule
import com.openrecall.relay.domain.model.CaptureSettings
import com.openrecall.relay.domain.model.DeviceStatus
import com.openrecall.relay.store.Config
import com.openrecall.relay.ui.copyToClipboard
import com.openrecall.relay.ui.design.DangerButton
import com.openrecall.relay.ui.design.RecallGroupLabel
import com.openrecall.relay.ui.design.RecallIcons
import com.openrecall.relay.ui.design.RecallScreenHeader
import com.openrecall.relay.ui.design.SecondaryButton
import com.openrecall.relay.ui.design.SettingsActionRow
import com.openrecall.relay.ui.design.SettingsGroup
import com.openrecall.relay.ui.design.SettingsToggleRow
import com.openrecall.relay.ui.design.SettingsValueRow

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
            initializer {
                SettingsViewModel(
                    RepositoryModule.repos.configuration,
                    RepositoryModule.repos.relaySettings,
                    RepositoryModule.repos.relayStarter,
                )
            }
        },
    )
    val config by vm.state.collectAsState()
    val capture by vm.capture.collectAsState()
    val device by vm.device.collectAsState()
    SettingsScreen(
        config = config,
        capture = capture,
        device = device,
        onCaptureChanged = vm::onCaptureChanged,
        onRetryCapture = vm::refresh,
        onReconfigure = onReconfigure,
        onForgetDevice = vm::forgetDevice,
        onGatewayPortChanged = vm::setGatewayPort,
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
    capture: CaptureUiState,
    device: DeviceStatus?,
    onCaptureChanged: (CaptureSettings) -> Unit,
    onRetryCapture: () -> Unit,
    onReconfigure: () -> Unit,
    onForgetDevice: () -> Unit,
    onGatewayPortChanged: (Int?) -> Unit,
    onOpenDevice: () -> Unit,
    onOpenCommands: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val colors = RecallTheme.colors
    val context = LocalContext.current
    var confirmForget by remember { mutableStateOf(false) }
    var editGatewayPort by remember { mutableStateOf(false) }

    Column(
        modifier = modifier
            .fillMaxSize()
            .background(colors.canvas)
            .verticalScroll(rememberScrollState())
            .statusBarsPadding()
            .padding(horizontal = 20.dp)
            .padding(bottom = 28.dp),
    ) {
        RecallScreenHeader(eyebrow = "Settings", hero = "Your relay, your rules")

        RecallGroupLabel("Device", Modifier.padding(top = 26.dp, bottom = 12.dp))
        SettingsGroup {
            SettingsValueRow(
                label = "Address",
                value = config.deviceAddress ?: "Not paired",
                onClick = config.deviceAddress?.let {
                    { copyToClipboard(context, "Address", it) }
                },
            )
            SettingsValueRow(
                label = "Status",
                value = if (config.provisioned) "Provisioned" else "Not set up",
            )
            // Connection freshness is the honest signal here and it leads.
            // The firmware keeps sending gap-marker packets while its voice
            // detector suppresses silence, so packets flowing means the
            // device is alive, not that anyone is talking — the two lines
            // below separate "alive and hearing speech", "alive in a quiet
            // room" and "gone".
            SettingsValueRow(
                label = "Link",
                value = linkLine(device),
            )
            SettingsValueRow(
                label = "Last speech",
                value = device?.lastTranscriptAgeS?.let { relativeAge(it) } ?: "—",
            )
            SettingsValueRow(
                label = "Battery",
                value = batteryLine(device),
                // Ships as a fixed server-side placeholder: battery sensing
                // does not exist at any layer of this product. Tagged rather
                // than rendered as a fact, because a plausible-looking
                // percentage is worse than a blank if anyone acts on it.
                onClick = null,
            )
            SettingsValueRow(
                label = "Firmware",
                value = device?.firmwareVersion ?: "Not reported",
                last = true,
            )
        }
        if (device != null && !device.measured) {
            Text(
                text = "The wearable reports no telemetry yet, so battery and firmware " +
                    "are placeholders. Everything above them is measured.",
                style = MaterialTheme.typography.bodySmall,
                color = colors.grey,
                modifier = Modifier.padding(top = 10.dp),
            )
        }

        RecallGroupLabel("Relay", Modifier.padding(top = 26.dp, bottom = 12.dp))
        SettingsGroup {
            SettingsValueRow(
                label = "URL",
                value = config.serverUrl.ifBlank { "—" },
                onClick = { copyToClipboard(context, "URL", config.serverUrl) },
            )
            SettingsValueRow(
                label = "Token",
                value = maskedToken(config.token),
                onClick = { copyToClipboard(context, "Token", config.token) },
            )
            SettingsValueRow(
                label = "Gateway port",
                value = config.gatewayPort?.toString() ?: "Default",
                onClick = { editGatewayPort = true },
                last = true,
            )
        }

        RecallGroupLabel("Capture", Modifier.padding(top = 26.dp, bottom = 12.dp))
        CaptureSection(
            capture = capture,
            onCaptureChanged = onCaptureChanged,
            onRetry = onRetryCapture,
        )

        RecallGroupLabel("Diagnostics", Modifier.padding(top = 26.dp, bottom = 12.dp))
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
            text = "OpenRecall Relay 0.1 · ${config.serverUrl.ifBlank { "no relay" }}",
            style = MaterialTheme.typography.bodySmall,
            color = colors.greyFaint,
            textAlign = TextAlign.Center,
            modifier = Modifier.padding(top = 16.dp).align(androidx.compose.ui.Alignment.CenterHorizontally),
        )
    }

    if (editGatewayPort) {
        GatewayPortDialog(
            current = config.gatewayPort,
            onDismiss = { editGatewayPort = false },
            onApply = {
                editGatewayPort = false
                onGatewayPortChanged(it)
            },
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

/**
 * The three capture toggles and the retention line.
 *
 * These write to the **relay**, not to this phone. That matters: a command
 * only reaches the wearable while it is connected, so the server holds the
 * desired state and converges the device on it at the next reconnect. The
 * toggles are therefore meaningful with the device switched off, which is
 * exactly when a phone-local preference would have been a no-op.
 *
 * Until the document loads the switches are inert — showing defaults the
 * server might not agree with would be a lie the user could act on.
 */
@Composable
private fun CaptureSection(
    capture: CaptureUiState,
    onCaptureChanged: (CaptureSettings) -> Unit,
    onRetry: () -> Unit,
) {
    val colors = RecallTheme.colors
    val settings = capture.settings
    if (settings == null) {
        SettingsGroup {
            SettingsValueRow(
                label = "Capture",
                value = if (capture.loading) "Loading…" else "Unavailable",
                last = true,
            )
        }
        if (!capture.loading) {
            Text(
                text = capture.error ?: "Couldn't read the relay's capture settings.",
                style = MaterialTheme.typography.bodySmall,
                color = colors.grey,
                modifier = Modifier.padding(top = 10.dp),
            )
            SecondaryButton(
                label = "Try again",
                onClick = onRetry,
                modifier = Modifier.padding(top = 10.dp),
            )
        }
        return
    }

    val c = settings.capture
    SettingsGroup {
        SettingsToggleRow(
            title = "Microphone",
            subtitle = "Let the wearable listen. Turning this off stops capture at " +
                "the device and at the relay.",
            checked = c.audioEnabled,
            onCheckedChange = { onCaptureChanged(c.copy(audioEnabled = it)) },
        )
        SettingsToggleRow(
            title = "Keep audio",
            subtitle = "Save the recordings themselves. Off still transcribes — you " +
                "get the text, without anything to play back.",
            checked = c.saveAudio,
            onCheckedChange = { onCaptureChanged(c.copy(saveAudio = it)) },
        )
        SettingsToggleRow(
            title = "Vision",
            subtitle = "Process images from the wearable's camera.",
            checked = c.visionEnabled,
            onCheckedChange = { onCaptureChanged(c.copy(visionEnabled = it)) },
        )
        SettingsValueRow(
            label = "Audio kept for",
            value = retentionLine(settings.retention.audioDays),
            last = true,
        )
    }
    Text(
        text = retentionNote(settings.retention.audioDays) +
            " These settings live on the relay, so they apply even while your " +
            "OpenRecall is offline.",
        style = MaterialTheme.typography.bodySmall,
        color = colors.grey,
        modifier = Modifier.padding(top = 10.dp),
    )
    if (capture.error != null) {
        Text(
            text = capture.error,
            style = MaterialTheme.typography.bodySmall,
            color = colors.danger,
            modifier = Modifier.padding(top = 8.dp),
        )
    }
}

private fun retentionLine(days: Int): String = when {
    days <= 0 -> "Not kept"
    days == 1 -> "1 day"
    else -> "$days days"
}

/**
 * Retention is tiered and the difference is not what a user assumes: audio
 * expires, the transcript and the memories drawn from it do not. Saying so
 * plainly is the point of this line.
 */
private fun retentionNote(days: Int): String = if (days <= 0) {
    "Audio isn't kept at all; transcripts and memories are."
} else {
    "Recordings are deleted after ${retentionLine(days).lowercase()}. " +
        "Their transcripts and memories are kept."
}

/** "Connected · heard 4s ago", or why not. */
private fun linkLine(device: DeviceStatus?): String {
    if (device == null) return "—"
    if (!device.relayConnected) return "Not connected"
    val age = device.lastPacketAgeS ?: return "Connected"
    return if (device.isFresh()) {
        "Connected · ${relativeAge(age)}"
    } else {
        // Packets flow continuously while the device is awake, so a long gap
        // means it stopped talking to us — the failure worth surfacing.
        "Connected · silent for ${relativeAge(age)}"
    }
}

private fun batteryLine(device: DeviceStatus?): String {
    val pct = device?.batteryPct ?: return "Not reported"
    val rendered = "${(pct * 100).toInt()}%"
    // The tag is load-bearing: without it the number reads as measured.
    return if (device.measured) rendered else "$rendered (estimated)"
}

/** "4s ago" / "3 min ago" / "2 h ago" for an age in seconds. */
private fun relativeAge(seconds: Double): String = when {
    seconds < 60 -> "${seconds.toInt()}s ago"
    seconds < 3600 -> "${(seconds / 60).toInt()} min ago"
    else -> "${(seconds / 3600).toInt()} h ago"
}

@Composable
private fun ChevronIcon() {
    Icon(
        RecallIcons.ChevronRight,
        contentDescription = null,
        tint = RecallTheme.colors.greyFaint,
        modifier = Modifier.size(18.dp),
    )
}

/** Mask the token for display — first and last three characters, middle hidden. */
private fun maskedToken(token: String): String = when {
    token.isBlank() -> "—"
    token.length <= 6 -> "•".repeat(token.length)
    else -> "${token.take(3)}${"•".repeat(6)}${token.takeLast(3)}"
}


/**
 * Edit the WebSocket gateway port, or clear it back to the URL's port.
 *
 * Provisioning reads this from the server's `/health`, which is correct on a
 * LAN and wrong behind a reverse proxy or a Cloudflare Tunnel: the server
 * advertises the port it binds (8765) while the public edge serves 443.
 * Re-running setup just rediscovers the same wrong value, so this is the only
 * way out without editing the server.
 *
 * Blank clears the override, which is a real setting rather than an absence —
 * it means "reuse the port already in the HTTP URL".
 */
@Composable
private fun GatewayPortDialog(
    current: Int?,
    onDismiss: () -> Unit,
    onApply: (Int?) -> Unit,
) {
    val colors = RecallTheme.colors
    var text by remember { mutableStateOf(current?.toString() ?: "") }
    val trimmed = text.trim()
    val parsed = trimmed.toIntOrNull()
    // Blank is valid and means "clear". A non-blank value must be a real port.
    val valid = trimmed.isEmpty() || (parsed != null && parsed in 1..65535)

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = colors.card,
        titleContentColor = colors.ink,
        textContentColor = colors.inkMuted,
        title = { Text("Gateway port") },
        text = {
            Column {
                Text(
                    "The port the relay opens its WebSocket on. Leave blank to " +
                        "reuse the port in the server URL — which is what you " +
                        "want behind a reverse proxy or a Cloudflare Tunnel, " +
                        "where the public port is 443.",
                )
                OutlinedTextField(
                    value = text,
                    onValueChange = { new -> text = new.filter { it.isDigit() } },
                    singleLine = true,
                    isError = !valid,
                    placeholder = { Text("Default") },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    modifier = Modifier.padding(top = 12.dp),
                )
                if (!valid) {
                    Text(
                        "Enter a port between 1 and 65535, or leave blank.",
                        style = MaterialTheme.typography.bodySmall,
                        color = colors.danger,
                        modifier = Modifier.padding(top = 6.dp),
                    )
                }
            }
        },
        confirmButton = {
            TextButton(
                enabled = valid,
                onClick = { onApply(if (trimmed.isEmpty()) null else parsed) },
            ) { Text("Apply", color = if (valid) colors.ink else colors.grey) }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) { Text("Cancel", color = colors.slate) }
        },
    )
}
