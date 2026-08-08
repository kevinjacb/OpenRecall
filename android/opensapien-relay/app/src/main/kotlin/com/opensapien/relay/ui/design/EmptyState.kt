package com.opensapien.relay.ui.design

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.opensapien.relay.core.ui.SenseTheme
import com.opensapien.relay.core.ui.Spacing

/**
 * Empty-state placeholder: optional icon, title, body, optional CTA. Centred
 * and narrow — the comp keeps empty copy short and never lets a CTA run the
 * full screen width.
 */
@Composable
fun EmptyState(
    title: String,
    body: String,
    modifier: Modifier = Modifier,
    icon: ImageVector? = null,
    ctaLabel: String? = null,
    onCta: (() -> Unit)? = null,
) {
    val colors = SenseTheme.colors
    Column(
        modifier = modifier
            .fillMaxWidth()
            .padding(Spacing.lg),
        verticalArrangement = Arrangement.spacedBy(Spacing.sm, Alignment.CenterVertically),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        if (icon != null) {
            Icon(
                imageVector = icon,
                contentDescription = null, // the title carries the meaning
                modifier = Modifier.size(40.dp),
                tint = colors.greyFaint,
            )
        }
        Text(
            title,
            style = MaterialTheme.typography.titleLarge,
            color = colors.ink,
            textAlign = TextAlign.Center,
        )
        Text(
            body,
            style = MaterialTheme.typography.bodyMedium,
            color = colors.grey,
            textAlign = TextAlign.Center,
        )
        if (ctaLabel != null && onCta != null) {
            SecondaryButton(
                label = ctaLabel,
                onClick = onCta,
                modifier = Modifier.padding(top = Spacing.sm).width(220.dp),
            )
        }
    }
}
