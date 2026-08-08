package com.openrecall.relay.ui.design

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyListScope
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.openrecall.relay.core.model.PagedResult

/**
 * LazyColumn-friendly vertical timeline. The dot column is fixed-width
 * and the content column takes the rest, so a long line of text wraps
 * cleanly without bumping the dot.
 *
 * LazyListScope extension is intentional: this composable is meant to
 * be used *inside* a LazyColumn, not as a top-level container, so the
 * outer column already has the right scroll/animation behavior.
 */
data class TimelineItem(
    val title: String,
    val subtitle: String? = null,
    val tone: Tone = Tone.Neutral,
)

fun LazyListScope.timeline(
    items: List<TimelineItem>,
    modifier: Modifier = Modifier,
    dotSize: Dp = 10.dp,
    contentPadding: Dp = 16.dp,
) {
    items(items, key = { it.title }) { item ->
        TimelineRow(item, dotSize, contentPadding, isLast = item == items.last())
    }
}

@Composable
private fun TimelineRow(
    item: TimelineItem,
    dotSize: Dp,
    contentPadding: Dp,
    isLast: Boolean,
) {
    val cs = MaterialTheme.colorScheme
    val dotColor = when (item.tone) {
        Tone.Neutral -> cs.onSurfaceVariant
        Tone.Muted -> cs.outline
        Tone.Accent -> cs.primary
    }
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = contentPadding, vertical = 4.dp),
        verticalAlignment = Alignment.Top,
    ) {
        // Leading dot + rail
        Column(
            modifier = Modifier.width(dotSize + 12.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Box(
                modifier = Modifier
                    .size(dotSize)
                    .clip(CircleShape)
                    .background(dotColor),
            )
            if (!isLast) {
                Spacer(Modifier.height(4.dp))
                Box(
                    modifier = Modifier
                        .width(2.dp)
                        .height(40.dp)
                        .background(cs.outline),
                )
            }
        }
        Spacer(Modifier.width(8.dp))
        Column(
            modifier = Modifier.padding(vertical = 2.dp),
            verticalArrangement = Arrangement.spacedBy(2.dp),
        ) {
            Text(
                text = item.title,
                style = MaterialTheme.typography.bodyMedium,
                color = cs.onSurface,
            )
            if (item.subtitle != null) {
                Text(
                    text = item.subtitle,
                    style = MaterialTheme.typography.bodySmall,
                    color = cs.onSurfaceVariant,
                )
            }
        }
    }
    if (!isLast) {
        HorizontalDivider(
            modifier = Modifier.padding(start = contentPadding + dotSize + 12.dp + 8.dp),
            color = cs.outline.copy(alpha = 0.5f),
        )
    }
}
