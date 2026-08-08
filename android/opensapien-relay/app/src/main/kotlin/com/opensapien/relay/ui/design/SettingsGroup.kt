package com.opensapien.relay.ui.design

import androidx.compose.animation.core.animateDpAsState
import androidx.compose.animation.animateColorAsState
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.opensapien.relay.core.ui.CardShape
import com.opensapien.relay.core.ui.PillShape
import com.opensapien.relay.core.ui.SenseTheme

/**
 * A grouped card of settings rows: one white surface, hairline border, and
 * dividers *between* rows only. Children should be [SettingsValueRow] /
 * [SettingsToggleRow] / [SettingsActionRow]; each draws its own bottom
 * divider except the last, which the group clips.
 */
@Composable
fun SettingsGroup(
    modifier: Modifier = Modifier,
    content: @Composable ColumnScope.() -> Unit,
) {
    val colors = SenseTheme.colors
    Column(
        modifier = modifier
            .fillMaxWidth()
            .clip(CardShape)
            .background(colors.card)
            .border(BorderStroke(1.dp, colors.border), CardShape),
        content = content,
    )
}

/** A read-only "label → value" row. Tap to copy, when [onClick] is given. */
@Composable
fun SettingsValueRow(
    label: String,
    value: String,
    modifier: Modifier = Modifier,
    last: Boolean = false,
    onClick: (() -> Unit)? = null,
) {
    val colors = SenseTheme.colors
    SettingsRowFrame(modifier = modifier, last = last, onClick = onClick) {
        Text(
            label,
            style = MaterialTheme.typography.bodyMedium,
            color = colors.inkMuted,
            modifier = Modifier.weight(1f),
        )
        Text(
            value,
            style = MaterialTheme.typography.titleSmall,
            color = colors.ink,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            textAlign = TextAlign.End,
            modifier = Modifier.weight(1.3f),
        )
    }
}

/** A tappable row with a title, a supporting line, and a trailing chevron slot. */
@Composable
fun SettingsActionRow(
    title: String,
    subtitle: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    last: Boolean = false,
    trailing: (@Composable () -> Unit)? = null,
) {
    val colors = SenseTheme.colors
    SettingsRowFrame(modifier = modifier, last = last, onClick = onClick) {
        Column(Modifier.weight(1f)) {
            Text(title, style = MaterialTheme.typography.titleSmall, color = colors.ink)
            Text(
                subtitle,
                style = MaterialTheme.typography.bodySmall,
                color = colors.grey,
                modifier = Modifier.padding(top = 3.dp),
            )
        }
        trailing?.invoke()
    }
}

/** A row whose trailing control is a switch. */
@Composable
fun SettingsToggleRow(
    title: String,
    subtitle: String,
    checked: Boolean,
    onCheckedChange: (Boolean) -> Unit,
    modifier: Modifier = Modifier,
    last: Boolean = false,
    trailing: (@Composable () -> Unit)? = null,
) {
    val colors = SenseTheme.colors
    SettingsRowFrame(
        modifier = modifier,
        last = last,
        onClick = { onCheckedChange(!checked) },
    ) {
        Column(Modifier.weight(1f)) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Text(title, style = MaterialTheme.typography.titleSmall, color = colors.ink)
                trailing?.invoke()
            }
            Text(
                subtitle,
                style = MaterialTheme.typography.bodySmall,
                color = colors.grey,
                modifier = Modifier.padding(top = 3.dp),
            )
        }
        SenseSwitch(checked = checked)
    }
}

/**
 * The comp's switch: a 44x26 pill whose 20dp knob slides between the ends.
 * Not an M3 `Switch` — that one draws an outline, a larger knob, and an
 * icon slot, none of which match.
 *
 * Presentational only; the enclosing row owns the click.
 */
@Composable
private fun SenseSwitch(checked: Boolean, modifier: Modifier = Modifier) {
    val colors = SenseTheme.colors
    val track by animateColorAsState(
        targetValue = if (checked) colors.ok else colors.trackOff,
        label = "switch-track",
    )
    val offset by animateDpAsState(
        targetValue = if (checked) 18.dp else 0.dp,
        label = "switch-knob",
    )
    Box(
        modifier
            .width(44.dp)
            .height(26.dp)
            .clip(PillShape)
            .background(track)
            .padding(3.dp),
        contentAlignment = Alignment.CenterStart,
    ) {
        Box(
            Modifier
                .padding(start = offset)
                .size(20.dp)
                .clip(CircleShape)
                .background(colors.card),
        )
    }
}

/** Shared padding, height and divider treatment for every settings row. */
@Composable
private fun SettingsRowFrame(
    modifier: Modifier,
    last: Boolean,
    onClick: (() -> Unit)?,
    content: @Composable androidx.compose.foundation.layout.RowScope.() -> Unit,
) {
    val colors = SenseTheme.colors
    Column(modifier.fillMaxWidth()) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .then(if (onClick != null) Modifier.clickable(onClick = onClick) else Modifier)
                .padding(horizontal = 18.dp, vertical = 15.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(14.dp),
            content = content,
        )
        if (!last) {
            Box(Modifier.fillMaxWidth().height(1.dp).background(colors.divider))
        }
    }
}
