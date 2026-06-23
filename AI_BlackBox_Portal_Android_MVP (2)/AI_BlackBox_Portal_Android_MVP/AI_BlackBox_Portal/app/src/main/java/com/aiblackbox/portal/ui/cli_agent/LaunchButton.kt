package com.aiblackbox.portal.ui.cli_agent

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.unit.dp
import com.aiblackbox.portal.ui.feedback.rememberPressFeedback

/**
 * Reusable launch button with an inline busy state.
 *
 * Built generically for reuse:
 *   - T20: SessionSwitcherTopBar dropdown items use [LeadingLaunchIcon]
 *          (the private icon-vs-spinner helper below) rather than a literal
 *          LaunchButton, because [androidx.compose.material3.DropdownMenuItem]
 *          has its own slot API.
 *   - T21: empty-state launch buttons render LaunchButton directly.
 *
 * Visual contract:
 *   - [isLoading] == true → button is functionally disabled (onClick suppressed),
 *     leading icon is replaced by a 16.dp [CircularProgressIndicator], and the
 *     label is dimmed to ~70% alpha to mirror Material disabled state without
 *     calling [androidx.compose.material3.Button] with `enabled = false`
 *     (which would flatten the surface tint and lose the loading affordance).
 *   - [isLoading] == false → standard Material 3 button with optional [icon].
 *
 * Hoisted state — busyness is owned by the caller (screen / VM).
 */
@Composable
fun LaunchButton(
    label: String,
    icon: ImageVector? = null,
    isLoading: Boolean = false,
    enabled: Boolean = true,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val feedback = rememberPressFeedback()
    Button(
        // `enabled` below already suppresses clicks when loading/disabled, so
        // no inner guard is needed — Material 3 won't dispatch onClick to a
        // disabled Button. Keep the single source of truth.
        onClick = { feedback(); onClick() },
        enabled = enabled && !isLoading,
        modifier = modifier,
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.Center,
        ) {
            LeadingLaunchIcon(
                isLoading = isLoading,
                icon = icon,
            )
            if (icon != null || isLoading) {
                Spacer(Modifier.width(8.dp))
            }
            Text(
                text = label,
                style = MaterialTheme.typography.labelLarge,
                modifier = Modifier.graphicsLayer { alpha = if (isLoading) 0.7f else 1.0f },
            )
        }
    }
}

/**
 * Shared icon-vs-spinner helper. Public to this package so SessionSwitcherTopBar's
 * DropdownMenuItem slots can use the same logic without recomputing it
 * inline. 24.dp matches the brief; the spinner colour follows
 * [LocalContentColor] so it inherits dark/light/accent depending on the
 * surface it lands on (button = onPrimary, dropdown = onSurface).
 */
@Composable
internal fun LeadingLaunchIcon(
    isLoading: Boolean,
    icon: ImageVector? = null,
    modifier: Modifier = Modifier,
) {
    if (isLoading) {
        CircularProgressIndicator(
            modifier = modifier.size(16.dp),
            strokeWidth = 2.dp,
            color = LocalContentColor.current,
        )
    } else if (icon != null) {
        Icon(
            imageVector = icon,
            contentDescription = null,
            modifier = modifier.size(20.dp),
            tint = LocalContentColor.current,
        )
    }
}
