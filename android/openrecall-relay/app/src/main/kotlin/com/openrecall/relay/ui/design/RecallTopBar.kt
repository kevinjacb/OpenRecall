package com.openrecall.relay.ui.design

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.openrecall.relay.core.ui.RecallTheme

/**
 * State for [RecallTopBar]. A data class rather than loose params so a future
 * version can add a subtitle or an overflow slot without breaking call sites.
 */
data class TopBarState(
    val title: String,
    val onBack: (() -> Unit)? = null,
)

/**
 * The screen top bar. The comp has no elevated app bar: the title sits on
 * the canvas at the same inset as the content, and a push screen gets a back
 * chevron on its left. This renders that, delegating to [RecallDetailHeader]
 * when there is somewhere to go back to.
 */
@Composable
fun RecallTopBar(state: TopBarState, modifier: Modifier = Modifier) {
    val colors = RecallTheme.colors
    Box(modifier.fillMaxWidth().background(colors.canvas)) {
        if (state.onBack != null) {
            RecallDetailHeader(title = state.title, onBack = state.onBack)
        } else {
            Box(
                Modifier
                    .fillMaxWidth()
                    .height(52.dp)
                    .padding(horizontal = 20.dp),
                contentAlignment = Alignment.CenterStart,
            ) {
                Text(
                    state.title,
                    style = MaterialTheme.typography.titleMedium,
                    color = colors.ink,
                )
            }
        }
    }
}
