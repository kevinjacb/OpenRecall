package com.opensapien.relay.ui.design

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.unit.dp
import com.opensapien.relay.core.ui.CardShape
import com.opensapien.relay.core.ui.SenseTheme

/**
 * The workhorse surface of the design: a white fill on a hairline border,
 * generously rounded. The comp uses a border rather than elevation — a
 * shadow at this radius reads as muddy on the warm canvas — so this
 * composable draws no shadow by design.
 *
 * [onClick] makes the whole card the touch target (the pattern used by
 * session rows and drill-down entries). Passing null renders a static card
 * with no ripple, which is what a read-only panel wants.
 */
@Composable
fun SenseCard(
    modifier: Modifier = Modifier,
    onClick: (() -> Unit)? = null,
    shape: Shape = CardShape,
    contentPadding: PaddingValues = PaddingValues(horizontal = 18.dp, vertical = 16.dp),
    content: @Composable ColumnScope.() -> Unit,
) {
    val colors = SenseTheme.colors
    Column(
        modifier = modifier
            .fillMaxWidth()
            .clip(shape)
            .background(colors.card)
            .border(BorderStroke(1.dp, colors.border), shape)
            .then(if (onClick != null) Modifier.clickable(onClick = onClick) else Modifier)
            .padding(contentPadding),
        content = content,
    )
}
