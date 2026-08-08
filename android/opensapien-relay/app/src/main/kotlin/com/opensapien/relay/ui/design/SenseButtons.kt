package com.opensapien.relay.ui.design

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.opensapien.relay.core.ui.ButtonShape
import com.opensapien.relay.core.ui.SenseTheme

/**
 * The comp's three button weights. Buttons are full-width by default
 * because that is how every one of them appears in the design; pass a
 * `Modifier.width(...)` to opt out.
 *
 *  - [PrimaryButton]   — ink fill, white label. One per screen.
 *  - [SecondaryButton] — white card fill on the hairline border.
 *  - [DangerButton]    — bare text in the destructive colour.
 */
@Composable
fun PrimaryButton(
    label: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
) {
    val colors = SenseTheme.colors
    Button(
        onClick = onClick,
        enabled = enabled,
        shape = ButtonShape,
        modifier = modifier.fillMaxWidth(),
        contentPadding = PaddingValues(horizontal = 20.dp, vertical = 16.dp),
        colors = ButtonDefaults.buttonColors(
            containerColor = colors.ink,
            contentColor = colors.card,
            disabledContainerColor = colors.track,
            disabledContentColor = colors.greyLight,
        ),
    ) {
        Text(label, style = MaterialTheme.typography.labelLarge)
    }
}

@Composable
fun SecondaryButton(
    label: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
) {
    val colors = SenseTheme.colors
    OutlinedButton(
        onClick = onClick,
        enabled = enabled,
        shape = ButtonShape,
        modifier = modifier.fillMaxWidth(),
        border = BorderStroke(1.dp, colors.border),
        contentPadding = PaddingValues(horizontal = 20.dp, vertical = 15.dp),
        colors = ButtonDefaults.outlinedButtonColors(
            containerColor = colors.card,
            contentColor = colors.ink,
            disabledContainerColor = colors.card,
            disabledContentColor = colors.greyLight,
        ),
    ) {
        Text(label, style = MaterialTheme.typography.labelLarge)
    }
}

@Composable
fun DangerButton(
    label: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val colors = SenseTheme.colors
    TextButton(
        onClick = onClick,
        modifier = modifier.fillMaxWidth(),
        colors = ButtonDefaults.textButtonColors(contentColor = colors.danger),
    ) {
        Text(
            label,
            style = MaterialTheme.typography.labelLarge,
            modifier = Modifier.padding(vertical = 4.dp),
        )
    }
}
