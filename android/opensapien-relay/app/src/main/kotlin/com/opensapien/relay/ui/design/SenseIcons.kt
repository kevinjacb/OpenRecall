package com.opensapien.relay.ui.design

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.PathParser
import androidx.compose.ui.unit.dp

/**
 * The icon set from the `design/` comp, transcribed from its SVG path data.
 *
 * Compose's bundled `material-icons-core` set has no sparkle, chat-bubble,
 * signal or waveform glyph, and the comp's icons are a distinct hairline
 * stroke style that the filled Material glyphs don't match. Pulling in
 * `material-icons-extended` to get approximate substitutes would add a large
 * dependency and still look wrong, so the paths are declared here verbatim.
 *
 * **Arc flags must be space-separated.** SVG lets the large-arc and sweep
 * flags run together with the next number (`a2 2 0 11-2.9 2.9`), and the
 * comp's exported paths do exactly that. Compose's [PathParser] does not
 * split them, so it reads `11` as the large-arc flag and silently produces a
 * mangled glyph — which is how the gear, chat bubble and signal icons first
 * rendered as unrecognisable slivers. Every arc below is written out as
 * `rx ry rot laf sf x y`. Keep it that way when adding paths.
 *
 * All are 24x24 icons; the stroked ones use round caps and joins. They carry
 * no colour of their own — `Icon(tint = …)` recolours them.
 */
object SenseIcons {

    /** Bottom bar: Home. */
    val Home: ImageVector = stroked(
        "Home",
        "M4 10.5 L12 4 l8 6.5 V19 a1.5 1.5 0 0 1 -1.5 1.5 h-13 A1.5 1.5 0 0 1 4 19 z",
        "M9.5 20.5 v-6 h5 v6",
    )

    /** Bottom bar: Recordings — a four-bar level meter. */
    val Recordings: ImageVector = stroked(
        "Recordings",
        "M6 10v4",
        "M10 6.5v11",
        "M14 4v16",
        "M18 9v6",
    )

    /** Bottom bar: Memories — a four-point sparkle. */
    val Memories: ImageVector = stroked(
        "Memories",
        "M12 3.5 l2.1 5.7 5.7 2.1 -5.7 2.1 L12 19.1 l-2.1 -5.7 L4.2 11.3 l5.7 -2.1 Z",
    )

    /** Bottom bar: Chat. */
    val Chat: ImageVector = stroked(
        "Chat",
        "M20 12 a7.5 7.5 0 0 1 -10.9 6.7 L4 20 l1.4 -4.3 A7.5 7.5 0 1 1 20 12 Z",
    )

    /** Bottom bar + Home header: Settings. */
    val Settings: ImageVector = stroked(
        "Settings",
        "M12 8.8 a3.2 3.2 0 1 0 0 6.4 a3.2 3.2 0 0 0 0 -6.4 Z",
        "M19.4 15 a1.7 1.7 0 0 0 .3 1.9 l.1 .1 a2 2 0 1 1 -2.9 2.9 l-.1 -.1 " +
            "a1.7 1.7 0 0 0 -1.9 -.3 a1.7 1.7 0 0 0 -1 1.5 v.2 a2 2 0 1 1 -4 0 v-.1 " +
            "a1.7 1.7 0 0 0 -1.1 -1.5 a1.7 1.7 0 0 0 -1.9 .3 l-.1 .1 a2 2 0 1 1 -2.9 -2.9 l.1 -.1 " +
            "a1.7 1.7 0 0 0 .3 -1.9 a1.7 1.7 0 0 0 -1.5 -1 H2.6 a2 2 0 1 1 0 -4 h.1 " +
            "a1.7 1.7 0 0 0 1.5 -1.1 a1.7 1.7 0 0 0 -.3 -1.9 l-.1 -.1 a2 2 0 1 1 2.9 -2.9 l.1 .1 " +
            "a1.7 1.7 0 0 0 1.9 .3 h.1 a1.7 1.7 0 0 0 1 -1.5 V2.6 a2 2 0 1 1 4 0 v.1 " +
            "a1.7 1.7 0 0 0 1 1.5 a1.7 1.7 0 0 0 1.9 -.3 l.1 -.1 a2 2 0 1 1 2.9 2.9 l-.1 .1 " +
            "a1.7 1.7 0 0 0 -.3 1.9 v.1 a1.7 1.7 0 0 0 1.5 1 h.2 a2 2 0 1 1 0 4 h-.1 " +
            "a1.7 1.7 0 0 0 -1.5 1 Z",
        strokeWidth = 1.7f,
    )

    /** Home stat tile: battery. */
    val Battery: ImageVector = stroked(
        "Battery",
        "M4.6 7 h10.8 A2.6 2.6 0 0 1 18 9.6 v4.8 a2.6 2.6 0 0 1 -2.6 2.6 H4.6 " +
            "A2.6 2.6 0 0 1 2 14.4 V9.6 A2.6 2.6 0 0 1 4.6 7 Z",
        "M21 10.5v3",
        strokeWidth = 1.7f,
    )

    /** Home stat tile: BLE signal strength. */
    val Signal: ImageVector = stroked(
        "Signal",
        "M5 12.5 a9.5 9.5 0 0 1 14 0",
        "M8.2 15.8 a5.2 5.2 0 0 1 7.6 0",
        "M12 18 a1 1 0 1 0 0 2 a1 1 0 0 0 0 -2 Z",
        strokeWidth = 1.7f,
    )

    /** Session detail + proactive messages: a small sparkle for "memories". */
    val Sparkle: ImageVector = stroked(
        "Sparkle",
        "M12 3 l1.9 5.2 L19 10 l-5.1 1.8 L12 17 l-1.9 -5.2 L5 10 l5.1 -1.8 Z",
        strokeWidth = 1.7f,
    )

    /** Search field affordance. */
    val Search: ImageVector = stroked(
        "Search",
        "M11 4.5 a6.5 6.5 0 1 0 0 13 a6.5 6.5 0 0 0 0 -13 Z",
        "M20 20 l-3.6 -3.6",
    )

    /** Chat composer: send. */
    val Send: ImageVector = stroked(
        "Send",
        "M5 12h13",
        "M12.5 6 l6 6 -6 6",
        strokeWidth = 1.9f,
    )

    /** Session player: play. */
    val Play: ImageVector = filled(
        "Play",
        "M8 5.6 c0 -.9 1 -1.4 1.7 -.9 l8 6.4 c.6 .5 .6 1.3 0 1.8 l-8 6.4 c-.7 .5 -1.7 0 -1.7 -.9 Z",
    )

    /** Session player: pause. */
    val Pause: ImageVector = filled(
        "Pause",
        "M7.2 4 h1.6 A1.2 1.2 0 0 1 10 5.2 v13.6 A1.2 1.2 0 0 1 8.8 20 H7.2 " +
            "A1.2 1.2 0 0 1 6 18.8 V5.2 A1.2 1.2 0 0 1 7.2 4 Z",
        "M15.2 4 h1.6 A1.2 1.2 0 0 1 18 5.2 v13.6 A1.2 1.2 0 0 1 16.8 20 H15.2 " +
            "A1.2 1.2 0 0 1 14 18.8 V5.2 A1.2 1.2 0 0 1 15.2 4 Z",
    )

    /** Memory row: the source link back to a session. */
    val Link: ImageVector = stroked(
        "Link",
        "M9 17 H7 a5 5 0 0 1 0 -10 h2",
        "M15 7 h2 a5 5 0 0 1 0 10 h-2",
        "M8 12h8",
    )

    /** Row affordance: drill in. */
    val ChevronRight: ImageVector = stroked(
        "ChevronRight",
        "M9 18 l6 -6 -6 -6",
    )
}

private fun stroked(
    name: String,
    vararg pathData: String,
    strokeWidth: Float = 1.8f,
): ImageVector = ImageVector.Builder(
    name = name,
    defaultWidth = 24.dp,
    defaultHeight = 24.dp,
    viewportWidth = 24f,
    viewportHeight = 24f,
).apply {
    pathData.forEach { d ->
        addPath(
            pathData = PathParser().parsePathString(d).toNodes(),
            stroke = SolidColor(Color.Black),
            strokeLineWidth = strokeWidth,
            strokeLineCap = StrokeCap.Round,
            strokeLineJoin = StrokeJoin.Round,
        )
    }
}.build()

private fun filled(
    name: String,
    vararg pathData: String,
): ImageVector = ImageVector.Builder(
    name = name,
    defaultWidth = 24.dp,
    defaultHeight = 24.dp,
    viewportWidth = 24f,
    viewportHeight = 24f,
).apply {
    pathData.forEach { d ->
        addPath(
            pathData = PathParser().parsePathString(d).toNodes(),
            fill = SolidColor(Color.Black),
        )
    }
}.build()
