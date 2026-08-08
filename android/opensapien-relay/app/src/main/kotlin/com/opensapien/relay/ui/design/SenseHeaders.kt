package com.opensapien.relay.ui.design

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.opensapien.relay.core.ui.SenseTheme

/**
 * The Home header: a small accent dot, the product name, and an optional
 * trailing settings affordance. Deliberately not an M3 `TopAppBar` — the
 * comp's header sits inside the scrolling content at the same horizontal
 * inset as the cards below it, which a TopAppBar cannot do.
 */
@Composable
fun SenseAppHeader(
    title: String,
    modifier: Modifier = Modifier,
    onSettings: (() -> Unit)? = null,
) {
    val colors = SenseTheme.colors
    Row(
        modifier = modifier.fillMaxWidth().height(44.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(9.dp),
        ) {
            Box(
                Modifier
                    .size(9.dp)
                    .clip(CircleShape)
                    .background(colors.accent),
            )
            Text(title, style = MaterialTheme.typography.titleMedium, color = colors.ink)
        }
        if (onSettings != null) {
            IconButton(onClick = onSettings, modifier = Modifier.size(34.dp)) {
                Icon(
                    SenseIcons.Settings,
                    contentDescription = "Settings",
                    tint = colors.slate,
                    modifier = Modifier.size(20.dp),
                )
            }
        }
    }
}

/**
 * The list-screen header: a small eyebrow line ("Recordings") above a large
 * editorial hero ("Everything it heard"), with an optional supporting line.
 */
@Composable
fun SenseScreenHeader(
    eyebrow: String,
    hero: String,
    modifier: Modifier = Modifier,
    supporting: String? = null,
) {
    val colors = SenseTheme.colors
    Column(modifier = modifier.fillMaxWidth()) {
        Box(Modifier.height(44.dp), contentAlignment = Alignment.CenterStart) {
            Text(eyebrow, style = MaterialTheme.typography.titleMedium, color = colors.ink)
        }
        Text(
            hero,
            style = MaterialTheme.typography.displaySmall,
            color = colors.ink,
            modifier = Modifier.padding(top = 4.dp),
        )
        if (supporting != null) {
            Text(
                supporting,
                style = MaterialTheme.typography.bodyMedium,
                color = colors.grey,
                modifier = Modifier.padding(top = 8.dp),
            )
        }
    }
}

/**
 * The push-screen header: back chevron, ellipsized title, optional trailing
 * slot. Used by session detail and the pairing flow.
 */
@Composable
fun SenseDetailHeader(
    title: String,
    onBack: () -> Unit,
    modifier: Modifier = Modifier,
    trailing: (@Composable () -> Unit)? = null,
) {
    val colors = SenseTheme.colors
    Row(
        modifier = modifier.fillMaxWidth().padding(horizontal = 4.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        IconButton(onClick = onBack, modifier = Modifier.size(40.dp)) {
            Icon(
                Icons.AutoMirrored.Filled.ArrowBack,
                contentDescription = "Back",
                tint = colors.ink,
                modifier = Modifier.size(20.dp),
            )
        }
        Text(
            title,
            style = MaterialTheme.typography.titleMedium,
            color = colors.ink,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f).padding(start = 4.dp),
        )
        trailing?.invoke()
    }
}

/**
 * The uppercase group label above a settings group or a date-grouped list
 * ("TODAY", "RELAY").
 */
@Composable
fun SenseGroupLabel(label: String, modifier: Modifier = Modifier) {
    Text(
        text = label.uppercase(),
        style = MaterialTheme.typography.labelSmall,
        color = SenseTheme.colors.greyLight,
        modifier = modifier,
    )
}
