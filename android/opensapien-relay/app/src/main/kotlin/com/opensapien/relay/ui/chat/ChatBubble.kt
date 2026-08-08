package com.opensapien.relay.ui.chat

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.opensapien.relay.core.ui.SenseTheme

/**
 * Who a bubble belongs to. Determines fill, border, corner asymmetry and
 * which side of the thread it hangs from.
 *
 *  - [You]       — ink fill, right-aligned, notched bottom-right.
 *  - [Agent]     — white card, left-aligned, notched bottom-left.
 *  - [Proactive] — accent-tinted card for a message the user didn't ask for.
 */
enum class BubbleAuthor { You, Agent, Proactive }

/** The bubble's own corner + colour recipe, from the comp. */
private data class BubbleStyle(
    val fill: Color,
    val content: Color,
    val border: Color,
    val shape: RoundedCornerShape,
    val alignment: Alignment.Horizontal,
    val maxWidthFraction: Float,
)

@Composable
private fun BubbleAuthor.style(): BubbleStyle {
    val c = SenseTheme.colors
    val mine = RoundedCornerShape(20.dp, 20.dp, 6.dp, 20.dp)
    val theirs = RoundedCornerShape(20.dp, 20.dp, 20.dp, 6.dp)
    return when (this) {
        BubbleAuthor.You -> BubbleStyle(c.ink, c.card, c.ink, mine, Alignment.End, 0.78f)
        BubbleAuthor.Agent -> BubbleStyle(c.card, c.inkSoft, c.border, theirs, Alignment.Start, 0.92f)
        BubbleAuthor.Proactive ->
            BubbleStyle(c.accentSofter, c.inkSoft, c.accentBorder, theirs, Alignment.Start, 0.92f)
    }
}

/**
 * The chat bubble frame. Every message variant composes this and supplies
 * its own body, so the geometry and colour rules live in exactly one place.
 *
 * [contentColor] is exposed to the body so text inside a [BubbleAuthor.You]
 * bubble can invert without each bubble re-deriving it.
 */
@Composable
fun ChatBubble(
    author: BubbleAuthor,
    modifier: Modifier = Modifier,
    content: @Composable ColumnScope.(contentColor: Color) -> Unit,
) {
    val style = author.style()
    Box(modifier.fillMaxWidth()) {
        Column(
            modifier = Modifier
                .align(
                    if (style.alignment == Alignment.End) {
                        Alignment.CenterEnd
                    } else {
                        Alignment.CenterStart
                    },
                )
                .fillMaxWidth(style.maxWidthFraction)
                .clip(style.shape)
                .background(style.fill)
                .border(BorderStroke(1.dp, style.border), style.shape)
                .padding(horizontal = 16.dp, vertical = 14.dp),
            verticalArrangement = Arrangement.spacedBy(7.dp),
        ) {
            content(style.content)
        }
    }
}
