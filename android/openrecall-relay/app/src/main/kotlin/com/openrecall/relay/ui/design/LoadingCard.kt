package com.openrecall.relay.ui.design

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.unit.dp
import com.openrecall.relay.core.ui.Spacing

/**
 * A placeholder card. Rendered as a card with two short shimmer rows —
 * a title bar and a value bar. The actual shimmer animation is wired
 * in via the [Modifier.shimmer] extension; today it's a static surface
 * variant color (see [shimmer] docs), and the visual contract is
 * preserved: "a card-shaped gray rectangle with two rows".
 */
@Composable
fun LoadingCard(modifier: Modifier = Modifier) {
    Card(
        modifier = modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surface,
            contentColor = MaterialTheme.colorScheme.onSurface,
        ),
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(Spacing.md),
        ) {
            Row {
                // Title bar — short, ~96dp wide.
                Box(modifier = Modifier
                    .height(12.dp)
                    .width(96.dp)
                    .clip(RoundedCornerShape(6.dp))
                    .shimmer())
                Spacer(Modifier.width(Spacing.sm))
                Box(modifier = Modifier
                    .height(12.dp)
                    .width(48.dp)
                    .clip(RoundedCornerShape(6.dp))
                    .shimmer())
            }
            Spacer(Modifier.height(Spacing.sm))
            // Value bar — long, ~70% width.
            Box(modifier = Modifier
                .height(28.dp)
                .fillMaxWidth(0.7f)
                .clip(RoundedCornerShape(6.dp))
                .shimmer())
        }
    }
}
