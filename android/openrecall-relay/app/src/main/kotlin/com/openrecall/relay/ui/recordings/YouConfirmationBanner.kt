package com.openrecall.relay.ui.recordings

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.openrecall.relay.core.ui.Spacing

/**
 * One-time "is this you? what should I call you?" prompt for the wearer.
 * Renders only while [state] is [YouConfirmationState.Prompting]; otherwise
 * it composes nothing. On submit it calls [onConfirm] with the trimmed name;
 * the caller (SessionDetailViewModel.confirmYou) sends the `name_speaker`
 * control and optimistically updates the cache. [onDismiss] dismisses the
 * prompt without naming.
 *
 * Architectural invariant: this composable MUST NOT import
 * `com.openrecall.relay.http.*` or `com.openrecall.relay.http.dto.*`
 * (enforced by ArchitecturalInvariantsTest).
 */
@Composable
fun YouConfirmationBanner(
    state: YouConfirmationState,
    onConfirm: (name: String) -> Unit,
    onDismiss: () -> Unit = {},
    modifier: Modifier = Modifier,
) {
    if (state !is YouConfirmationState.Prompting) return
    var draft by remember { mutableStateOf("") }

    Surface(
        shape = RoundedCornerShape(12.dp),
        color = MaterialTheme.colorScheme.primaryContainer,
        contentColor = MaterialTheme.colorScheme.onPrimaryContainer,
        modifier = modifier
            .fillMaxWidth()
            .padding(Spacing.sm)
            .testTag("you_confirm_banner"),
    ) {
        Column(modifier = Modifier.padding(Spacing.md)) {
            Text(
                text = "Is this you? What should I call you?",
                style = MaterialTheme.typography.titleSmall,
            )
            Spacer(Modifier.height(Spacing.xs))
            Row(verticalAlignment = Alignment.CenterVertically) {
                OutlinedTextField(
                    value = draft,
                    onValueChange = { draft = it },
                    singleLine = true,
                    placeholder = { Text("Your name") },
                    modifier = Modifier
                        .weight(1f)
                        .testTag("you_confirm_field"),
                )
                Spacer(Modifier.width(Spacing.xs))
                Button(
                    onClick = {
                        val name = draft.trim()
                        if (name.isNotEmpty()) onConfirm(name)
                    },
                    enabled = draft.isNotBlank(),
                    modifier = Modifier.testTag("you_confirm_submit"),
                ) { Text("Save") }
            }
        }
    }
}