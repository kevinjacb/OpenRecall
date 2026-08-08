package com.opensapien.relay.ui

import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.opensapien.relay.core.ui.SenseTheme
import com.opensapien.relay.setup.SetupStep
import com.opensapien.relay.ui.design.DeviceVisual
import com.opensapien.relay.ui.design.DeviceVisualState
import com.opensapien.relay.ui.design.PrimaryButton
import com.opensapien.relay.ui.design.SenseCard
import com.opensapien.relay.ui.design.SenseDetailHeader
import com.opensapien.relay.ui.design.SenseTextField
import com.opensapien.relay.ui.design.StatusBadge
import com.opensapien.relay.ui.design.StatusTone

/**
 * The pairing wizard — the comp's "Set up your Sense".
 *
 * One screen throughout: the device hero at the top with its LED reflecting
 * the current step, a headline and supporting line, and whatever the step
 * needs below. The form is only shown on [SetupStep.EnterServer]; the later
 * steps replace it with progress, because there is nothing for the user to
 * do while the BLE scan and provisioning run.
 *
 * [onCancel] is null on first run (there is nowhere to go back to) and
 * non-null when the wizard was launched from Settings.
 */
@Composable
fun SetupScreen(
    step: SetupStep,
    onCancel: (() -> Unit)? = null,
    onServer: (String, String) -> Unit,
) {
    val colors = SenseTheme.colors
    Column(
        Modifier
            .fillMaxSize()
            .background(colors.canvas)
            .verticalScroll(rememberScrollState())
            .statusBarsPadding()
            .imePadding(),
    ) {
        if (onCancel != null) {
            SenseDetailHeader(title = "Set up your device", onBack = onCancel)
        } else {
            Text(
                text = "Set up your device",
                style = MaterialTheme.typography.titleMedium,
                color = colors.ink,
                modifier = Modifier.padding(horizontal = 20.dp, vertical = 14.dp),
            )
        }

        DeviceVisual(state = step.visualState(), height = 240.dp)

        Column(
            Modifier.fillMaxWidth().padding(horizontal = 20.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(
                text = step.headline(),
                style = MaterialTheme.typography.headlineMedium,
                color = colors.ink,
                textAlign = TextAlign.Center,
            )
            Text(
                text = step.supporting(),
                style = MaterialTheme.typography.bodyMedium,
                color = colors.grey,
                textAlign = TextAlign.Center,
                modifier = Modifier.padding(top = 6.dp),
            )
        }

        AnimatedContent(
            targetState = step,
            transitionSpec = { fadeIn() togetherWith fadeOut() },
            label = "setup-step",
            modifier = Modifier.padding(horizontal = 20.dp, vertical = 24.dp),
        ) { s ->
            when (s) {
                is SetupStep.EnterServer -> ServerForm(s, onServer)
                is SetupStep.Connecting -> Progress(s.msg)
                is SetupStep.Scanning -> ScanProgress(s)
                is SetupStep.Provisioning -> Progress(s.msg)
                is SetupStep.Done -> Progress("Relay running")
            }
        }
    }
}

@Composable
private fun ServerForm(step: SetupStep.EnterServer, onServer: (String, String) -> Unit) {
    val colors = SenseTheme.colors
    // rememberSaveable so a rotation mid-typing doesn't wipe a pasted token.
    // Keyed on the step's values so a failed attempt re-seeds the fields with
    // what the ViewModel persisted.
    var url by rememberSaveable(step.url) {
        mutableStateOf(step.url.ifEmpty { "http://" })
    }
    var token by rememberSaveable(step.token) { mutableStateOf(step.token) }

    Column(verticalArrangement = Arrangement.spacedBy(14.dp)) {
        SenseTextField(
            value = url,
            onValueChange = { url = it },
            label = "Server URL",
            placeholder = "http://192.168.1.20:8766",
        )
        SenseTextField(
            value = token,
            onValueChange = { token = it },
            label = "Bearer token",
            placeholder = "Paste the relay's token",
        )
        if (step.error != null) {
            SenseCard {
                Text(
                    text = step.error,
                    style = MaterialTheme.typography.bodyMedium,
                    color = colors.danger,
                )
            }
        }
        PrimaryButton(
            label = "Connect device",
            onClick = { onServer(url.trim(), token.trim()) },
            enabled = url.isNotBlank() && token.isNotBlank(),
            modifier = Modifier.padding(top = 6.dp),
        )
    }
}

@Composable
private fun ScanProgress(step: SetupStep.Scanning) {
    val colors = SenseTheme.colors
    Column(
        Modifier.fillMaxWidth(),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        if (step.error != null) {
            Text(
                text = step.error,
                style = MaterialTheme.typography.bodyMedium,
                color = colors.danger,
                textAlign = TextAlign.Center,
            )
        } else {
            StatusBadge(
                label = step.devices.firstOrNull()?.let { "Found $it" } ?: "Scanning…",
                tone = StatusTone.Working,
            )
        }
    }
}

@Composable
private fun Progress(message: String) {
    Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally) {
        StatusBadge(label = message, tone = StatusTone.Working)
    }
}

/** The LED state that matches each step — amber while working, green once done. */
private fun SetupStep.visualState(): DeviceVisualState = when (this) {
    is SetupStep.EnterServer -> DeviceVisualState.Off
    is SetupStep.Done -> DeviceVisualState.Connected
    else -> DeviceVisualState.Searching
}

private fun SetupStep.headline(): String = when (this) {
    is SetupStep.EnterServer -> "Point it at your relay"
    is SetupStep.Connecting -> "Checking your relay"
    is SetupStep.Scanning -> "Looking for your OpenSapien"
    is SetupStep.Provisioning -> "Handing over the keys"
    is SetupStep.Done -> "You're all set"
}

private fun SetupStep.supporting(): String = when (this) {
    is SetupStep.EnterServer ->
        "Tell your device where to send what it hears. Both values come from the relay you're running."
    is SetupStep.Connecting -> "Making sure the relay answers and the token is good."
    is SetupStep.Scanning ->
        "Hold the button until the light breathes amber, then keep it near your phone."
    is SetupStep.Provisioning -> "Writing the relay's public key to the device."
    is SetupStep.Done -> "Your OpenSapien is paired and the relay is running."
}
