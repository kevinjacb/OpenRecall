package com.sense.relay.ui
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.sense.relay.setup.SetupStep

@Composable
fun SetupScreen(step: SetupStep, onServer: (String, String) -> Unit) {
    Surface(Modifier.fillMaxSize()) {
        Column(Modifier.padding(24.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
            Text("Set up your Sense", style = MaterialTheme.typography.headlineMedium)
            AnimatedContent(step, transitionSpec = { fadeIn() togetherWith fadeOut() }) { s ->
                when (s) {
                    is SetupStep.EnterServer -> ServerForm(s, onServer)
                    is SetupStep.Connecting -> StatusText(s.msg)
                    is SetupStep.Scanning -> ScanView(s)
                    is SetupStep.Provisioning -> StatusText(s.msg)
                    is SetupStep.Done -> StatusText("Relay running")
                }
            }
        }
    }
}

@Composable private fun ServerForm(s: SetupStep.EnterServer, onServer: (String, String) -> Unit) {
    var url by remember { mutableStateOf(s.url.ifEmpty { "https://" }) }
    var token by remember { mutableStateOf(s.token) }
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        OutlinedTextField(url, { url = it }, label = { Text("Server URL") }, singleLine = true)
        OutlinedTextField(token, { token = it }, label = { Text("Bearer token") }, singleLine = true)
        s.error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        Button({ onServer(url.trim(), token.trim()) }) { Text("Connect") }
    }
}

@Composable private fun ScanView(s: SetupStep.Scanning) {
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        if (s.devices.isNotEmpty()) Text("Found device: ${s.devices.first()}") else Text("Scanning…")
        s.error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
    }
}

@Composable private fun StatusText(msg: String) { Text(msg) }