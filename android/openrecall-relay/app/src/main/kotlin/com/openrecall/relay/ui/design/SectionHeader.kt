package com.openrecall.relay.ui.design

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import com.openrecall.relay.core.ui.RecallTheme

/**
 * Section header — a title with an optional trailing action, e.g.
 * "Recent sessions ————— See all".
 */
@Composable
fun SectionHeader(
    title: String,
    modifier: Modifier = Modifier,
    trailing: (@Composable () -> Unit)? = null,
) {
    Row(
        modifier = modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = title,
            style = MaterialTheme.typography.titleLarge,
            color = RecallTheme.colors.ink,
        )
        trailing?.invoke()
    }
}

/**
 * The trailing action for a [SectionHeader] — accent text with no button
 * chrome (the comp's "See all"). A composable rather than a bare `Text` so
 * the accent colour and weight stay in one place.
 */
@Composable
fun SectionHeaderAction(label: String, onClick: () -> Unit, modifier: Modifier = Modifier) {
    Text(
        text = label,
        style = MaterialTheme.typography.labelMedium,
        color = RecallTheme.colors.accent,
        modifier = modifier.clickable(onClick = onClick),
    )
}
