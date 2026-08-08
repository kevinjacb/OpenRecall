package com.opensapien.relay.ui.design

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Icon
import androidx.compose.material3.LocalTextStyle
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import com.opensapien.relay.core.ui.FieldShape
import com.opensapien.relay.core.ui.SenseTheme

/**
 * The comp's text input: a white field on a hairline border with an
 * uppercase label sitting *above* the box (not floating inside it, which is
 * why this isn't an M3 `OutlinedTextField`).
 */
@Composable
fun SenseTextField(
    value: String,
    onValueChange: (String) -> Unit,
    modifier: Modifier = Modifier,
    label: String? = null,
    placeholder: String = "",
    singleLine: Boolean = true,
    enabled: Boolean = true,
    imeAction: ImeAction = ImeAction.Default,
    keyboardActions: KeyboardActions = KeyboardActions.Default,
) {
    Column(modifier = modifier.fillMaxWidth()) {
        if (label != null) {
            SenseGroupLabel(label, Modifier.padding(bottom = 8.dp))
        }
        SenseFieldBox(shape = FieldShape) {
            SenseBasicField(
                value = value,
                onValueChange = onValueChange,
                placeholder = placeholder,
                singleLine = singleLine,
                enabled = enabled,
                imeAction = imeAction,
                keyboardActions = keyboardActions,
                modifier = Modifier.weight(1f),
            )
        }
    }
}

/**
 * The search field used on Recordings: a leading magnifier inside the same
 * bordered box.
 */
@Composable
fun SenseSearchField(
    value: String,
    onValueChange: (String) -> Unit,
    modifier: Modifier = Modifier,
    placeholder: String = "Search",
    onSearch: () -> Unit = {},
) {
    val colors = SenseTheme.colors
    SenseFieldBox(modifier = modifier, shape = FieldShape) {
        Icon(
            SenseIcons.Search,
            contentDescription = null,
            tint = colors.greyLight,
            modifier = Modifier.size(18.dp),
        )
        SenseBasicField(
            value = value,
            onValueChange = onValueChange,
            placeholder = placeholder,
            singleLine = true,
            enabled = true,
            imeAction = ImeAction.Search,
            keyboardActions = KeyboardActions(onSearch = { onSearch() }),
            modifier = Modifier.weight(1f),
        )
    }
}

/** The bordered white box every field sits in. */
@Composable
private fun SenseFieldBox(
    modifier: Modifier = Modifier,
    shape: Shape = FieldShape,
    content: @Composable androidx.compose.foundation.layout.RowScope.() -> Unit,
) {
    val colors = SenseTheme.colors
    Row(
        modifier = modifier
            .fillMaxWidth()
            .clip(shape)
            .background(colors.card)
            .border(BorderStroke(1.dp, colors.border), shape)
            .padding(horizontal = 16.dp, vertical = 14.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(10.dp),
        content = content,
    )
}

/**
 * The bare editor. Uses [BasicTextField] rather than an M3 field so there is
 * no built-in container, indicator line, or label chrome to fight with.
 */
@Composable
private fun SenseBasicField(
    value: String,
    onValueChange: (String) -> Unit,
    placeholder: String,
    singleLine: Boolean,
    enabled: Boolean,
    imeAction: ImeAction,
    keyboardActions: KeyboardActions,
    modifier: Modifier = Modifier,
) {
    val colors = SenseTheme.colors
    Box(modifier = modifier, contentAlignment = Alignment.CenterStart) {
        if (value.isEmpty()) {
            Text(
                placeholder,
                style = MaterialTheme.typography.bodyMedium,
                color = colors.greyLight,
            )
        }
        BasicTextField(
            value = value,
            onValueChange = onValueChange,
            enabled = enabled,
            singleLine = singleLine,
            maxLines = if (singleLine) 1 else 5,
            textStyle = LocalTextStyle.current.merge(
                MaterialTheme.typography.bodyMedium.copy(color = colors.ink),
            ),
            cursorBrush = SolidColor(colors.accent),
            keyboardOptions = KeyboardOptions(imeAction = imeAction),
            keyboardActions = keyboardActions,
            modifier = Modifier.fillMaxWidth(),
        )
    }
}
