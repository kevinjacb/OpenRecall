package com.sense.relay.core.ui

import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Shapes
import androidx.compose.ui.unit.dp

/**
 * Rounded-corner tokens. Picked for a "soft, premium, monochrome" feel —
 * not the M3 default (which leans more conservative). Cards lean extraLarge;
 * chips and pills lean small/medium.
 */
val SenseShapes: Shapes = Shapes(
    extraSmall = RoundedCornerShape(4.dp),
    small = RoundedCornerShape(8.dp),
    medium = RoundedCornerShape(12.dp),
    large = RoundedCornerShape(20.dp),
    extraLarge = RoundedCornerShape(28.dp),
)
