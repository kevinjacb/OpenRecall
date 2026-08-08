package com.opensapien.relay.ui.design

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.opensapien.relay.core.ui.PillShape
import com.opensapien.relay.core.ui.SenseTheme

/**
 * A soft accent-tinted chip. The comp uses it for memory tags on the session
 * detail and for the "4 memories" count on a recording row — always a
 * read-only label, never an action.
 */
@Composable
fun AccentChip(
    label: String,
    modifier: Modifier = Modifier,
    onClick: (() -> Unit)? = null,
) {
    val colors = SenseTheme.colors
    Text(
        text = label,
        style = MaterialTheme.typography.labelMedium,
        color = colors.accentInk,
        maxLines = 1,
        overflow = TextOverflow.Ellipsis,
        modifier = modifier
            .clip(PillShape)
            .background(colors.accentSoft)
            .then(if (onClick != null) Modifier.clickable(onClick = onClick) else Modifier)
            .padding(horizontal = 12.dp, vertical = 7.dp),
    )
}

/**
 * A selectable filter chip. Selected inverts to the ink fill; unselected is
 * a white card on the hairline border — the same two weights the comp uses
 * for the Memories filter row.
 */
@Composable
fun FilterPill(
    label: String,
    selected: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val colors = SenseTheme.colors
    val fill = if (selected) colors.ink else colors.card
    val ink = if (selected) colors.card else colors.inkMuted
    val border = if (selected) colors.ink else colors.border
    Text(
        text = label,
        style = MaterialTheme.typography.labelMedium,
        color = ink,
        maxLines = 1,
        modifier = modifier
            .clip(PillShape)
            .background(fill)
            .border(BorderStroke(1.dp, border), PillShape)
            .clickable(onClick = onClick)
            .padding(horizontal = 15.dp, vertical = 9.dp),
    )
}
