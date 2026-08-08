package com.openrecall.relay.ui.nav

import androidx.compose.animation.animateColorAsState
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.selection.selectable
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.unit.dp
import com.openrecall.relay.core.ui.PillShape
import com.openrecall.relay.core.ui.RecallTheme
import com.openrecall.relay.ui.design.RecallIcons

/**
 * The five bottom-bar destinations, in the comp's order.
 *
 * Memories is a tab here (it was previously reachable only as a deep-link
 * from a chat refusal). The `design/` comp gives it a first-class slot —
 * "what it decided to keep" is one of the product's two read surfaces, and
 * burying it behind a refusal made it effectively undiscoverable.
 *
 * Device and Commands remain off the bar: they are diagnostic surfaces,
 * reached from Settings.
 */
internal val mainDestinations: List<MainTab> = listOf(
    MainTab(Destination.Home, "Home", RecallIcons.Home),
    MainTab(Destination.Recordings, "Recordings", RecallIcons.Recordings),
    MainTab(Destination.Memory, "Memories", RecallIcons.Memories),
    MainTab(Destination.Chat, "Chat", RecallIcons.Chat),
    MainTab(Destination.Settings, "Settings", RecallIcons.Settings),
)

internal data class MainTab(
    val destination: Destination,
    val label: String,
    val icon: ImageVector,
)

/**
 * The bottom navigation bar. Not an M3 `NavigationBar`: the comp's selected
 * state is a warm accent pill behind the icon with the label always visible
 * beneath, on the canvas colour with a hairline top rule — which the M3
 * component's indicator, elevation and tonal surface all fight.
 *
 * The parent owns the NavController so back-stack behaviour lives in one
 * place; this takes the current route and reports selections.
 */
@Composable
fun BottomBar(
    currentRoute: String?,
    onSelect: (Destination) -> Unit,
    modifier: Modifier = Modifier,
) {
    val colors = RecallTheme.colors
    Column(modifier.fillMaxWidth().background(colors.canvas)) {
        Box(Modifier.fillMaxWidth().height(1.dp).background(colors.divider))
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .navigationBarsPadding()
                .padding(start = 8.dp, end = 8.dp, top = 8.dp, bottom = 10.dp),
            horizontalArrangement = Arrangement.spacedBy(2.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            mainDestinations.forEach { tab ->
                TabItem(
                    tab = tab,
                    selected = currentRoute == tab.destination.route,
                    onClick = { onSelect(tab.destination) },
                    modifier = Modifier.weight(1f),
                )
            }
        }
    }
}

@Composable
private fun TabItem(
    tab: MainTab,
    selected: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val colors = RecallTheme.colors
    val indicator by animateColorAsState(
        targetValue = if (selected) colors.accentTab else Color.Transparent,
        label = "tab-indicator",
    )
    val content by animateColorAsState(
        targetValue = if (selected) colors.ink else colors.slate,
        label = "tab-content",
    )
    Column(
        modifier = modifier
            .selectable(selected = selected, role = Role.Tab, onClick = onClick)
            .padding(vertical = 4.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(5.dp),
    ) {
        Box(
            Modifier
                .width(56.dp)
                .height(30.dp)
                .clip(PillShape)
                .background(indicator),
            contentAlignment = Alignment.Center,
        ) {
            Icon(
                tab.icon,
                contentDescription = null, // the label below is the accessible name
                tint = content,
                modifier = Modifier.size(21.dp),
            )
        }
        Text(tab.label, style = MaterialTheme.typography.labelSmall, color = content)
    }
}
