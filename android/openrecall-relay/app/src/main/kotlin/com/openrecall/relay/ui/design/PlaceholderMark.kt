package com.openrecall.relay.ui.design

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Info
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.unit.dp
import com.openrecall.relay.core.ui.PillShape
import com.openrecall.relay.core.ui.RecallTheme
import com.openrecall.relay.core.ui.TileShape

/**
 * Marks a surface that exists in the `design/` comp but has no data source
 * in the app yet.
 *
 * Being explicit beats faking it: a tile showing an invented "82%" battery
 * reads as a bug, whereas one showing "—" next to this mark reads as an
 * honest gap. Every placeholder in the redesign carries one of these two
 * affordances, so `grep PlaceholderTag` / `PlaceholderNote` lists exactly
 * what still needs wiring.
 */
@Composable
fun PlaceholderTag(modifier: Modifier = Modifier, label: String = "Placeholder") {
    val colors = RecallTheme.colors
    Text(
        text = label.uppercase(),
        style = MaterialTheme.typography.labelSmall,
        color = colors.greyLight,
        modifier = modifier
            .clip(PillShape)
            .background(colors.canvasSunken)
            .padding(horizontal = 8.dp, vertical = 3.dp),
    )
}

/**
 * The block form: a muted, dashed-feeling panel explaining what will live
 * here. Used where a whole card in the comp has no backing endpoint.
 */
@Composable
fun PlaceholderNote(
    title: String,
    body: String,
    modifier: Modifier = Modifier,
) {
    val colors = RecallTheme.colors
    Column(
        modifier = modifier
            .fillMaxWidth()
            .clip(TileShape)
            .background(colors.canvasSunken)
            .border(BorderStroke(1.dp, colors.border), TileShape)
            .padding(PaddingValues(horizontal = 16.dp, vertical = 14.dp)),
        verticalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Icon(
                Icons.Filled.Info,
                contentDescription = null,
                tint = colors.greyLight,
                modifier = Modifier.size(15.dp),
            )
            Text(title, style = MaterialTheme.typography.titleSmall, color = colors.slate)
            PlaceholderTag()
        }
        Text(body, style = MaterialTheme.typography.bodySmall, color = colors.grey)
    }
}
