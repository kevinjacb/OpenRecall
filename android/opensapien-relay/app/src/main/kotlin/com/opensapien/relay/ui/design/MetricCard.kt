package com.opensapien.relay.ui.design

import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.opensapien.relay.core.ui.SenseTheme

/**
 * Label + big number + optional subtitle, on a [SenseCard]. The number
 * dominates; the subtitle sits below in the muted tone so a line of context
 * doesn't compete with it.
 *
 * [content] is a trailing slot so a caller can drop in a badge or meter
 * without needing a new MetricCard variant.
 */
@Composable
fun MetricCard(
    title: String,
    value: String,
    subtitle: String? = null,
    modifier: Modifier = Modifier,
    content: @Composable () -> Unit = {},
) {
    val colors = SenseTheme.colors
    SenseCard(modifier = modifier) {
        Text(title, style = MaterialTheme.typography.labelSmall, color = colors.grey)
        Text(
            value,
            style = MaterialTheme.typography.headlineMedium,
            color = colors.ink,
            modifier = Modifier.padding(top = 6.dp),
        )
        if (subtitle != null) {
            Text(
                subtitle,
                style = MaterialTheme.typography.bodySmall,
                color = colors.grey,
                modifier = Modifier.padding(top = 4.dp),
            )
        }
        content()
    }
}
