package com.openrecall.relay.ui.design

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.unit.dp
import com.openrecall.relay.core.ui.RecallTheme
import com.openrecall.relay.core.ui.TileShape

/**
 * A small stat tile — icon + label, a large value, and an optional meter
 * beneath. The comp pairs two of these side by side on Home (Battery and
 * BLE link).
 *
 * [meter] is the trailing slot; use [TileMeter] for a continuous 0..1 bar or
 * [TileSegments] for a discrete signal-strength readout.
 */
@Composable
fun StatTile(
    label: String,
    value: String,
    icon: ImageVector,
    modifier: Modifier = Modifier,
    meter: (@Composable () -> Unit)? = null,
) {
    val colors = RecallTheme.colors
    RecallCard(
        modifier = modifier,
        shape = TileShape,
        contentPadding = PaddingValues(horizontal = 16.dp, vertical = 15.dp),
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(7.dp),
        ) {
            Icon(icon, contentDescription = null, tint = colors.grey, modifier = Modifier.size(15.dp))
            Text(label, style = MaterialTheme.typography.labelSmall, color = colors.grey)
        }
        Text(
            value,
            style = MaterialTheme.typography.headlineSmall,
            color = colors.ink,
            modifier = Modifier.padding(top = 9.dp),
        )
        if (meter != null) {
            Box(Modifier.padding(top = 9.dp)) { meter() }
        }
    }
}

/** A continuous meter. [fraction] is clamped to 0..1. */
@Composable
fun TileMeter(fraction: Float, color: Color, modifier: Modifier = Modifier) {
    val colors = RecallTheme.colors
    Box(
        modifier
            .fillMaxWidth()
            .height(4.dp)
            .clip(RoundedCornerShape(2.dp))
            .background(colors.track),
    ) {
        Box(
            Modifier
                .fillMaxWidth(fraction.coerceIn(0f, 1f))
                .fillMaxHeight()
                .clip(RoundedCornerShape(2.dp))
                .background(color),
        )
    }
}

/**
 * A discrete meter: [filled] of [total] segments in the ink colour, the rest
 * in the track colour. The comp uses 4 segments for BLE signal strength.
 */
@Composable
fun TileSegments(filled: Int, modifier: Modifier = Modifier, total: Int = 4) {
    val colors = RecallTheme.colors
    Row(
        modifier.fillMaxWidth().height(4.dp),
        horizontalArrangement = Arrangement.spacedBy(3.dp),
    ) {
        repeat(total) { index ->
            Box(
                Modifier
                    .weight(1f)
                    .fillMaxHeight()
                    .clip(RoundedCornerShape(2.dp))
                    .background(if (index < filled) colors.ink else colors.track),
            )
        }
    }
}
