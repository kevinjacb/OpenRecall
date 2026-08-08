package com.openrecall.relay.core.ui

import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Shapes
import androidx.compose.ui.unit.dp

/**
 * Corner tokens from the `design/` comp. The comp is generous with radii:
 * cards sit at 20dp, inputs and small tiles at 14–18dp, and anything
 * pill-shaped uses [PillShape].
 */
val RecallShapes: Shapes = Shapes(
    extraSmall = RoundedCornerShape(8.dp),
    small = RoundedCornerShape(12.dp),
    medium = RoundedCornerShape(14.dp),
    large = RoundedCornerShape(18.dp),
    extraLarge = RoundedCornerShape(20.dp),
)

/** Fully rounded — status pills, chips, the tab indicator, the composer. */
val PillShape = RoundedCornerShape(percent = 50)

/** The standard card radius (comp: 20px). */
val CardShape = RoundedCornerShape(20.dp)

/** Smaller card / tile radius (comp: 18px). */
val TileShape = RoundedCornerShape(18.dp)

/** Text inputs and search fields (comp: 14px). */
val FieldShape = RoundedCornerShape(14.dp)

/** Filled primary buttons (comp: 16px). */
val ButtonShape = RoundedCornerShape(16.dp)
