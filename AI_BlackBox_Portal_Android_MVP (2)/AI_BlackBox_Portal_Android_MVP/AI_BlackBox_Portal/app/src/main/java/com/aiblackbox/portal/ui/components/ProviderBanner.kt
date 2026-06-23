package com.aiblackbox.portal.ui.components

import android.view.HapticFeedbackConstants
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateContentSize
import androidx.compose.animation.expandVertically
import androidx.compose.animation.shrinkVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import com.aiblackbox.portal.ui.feedback.clickFeedback
import com.aiblackbox.portal.ui.feedback.performPressFeedback
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.Neutral100
import com.aiblackbox.portal.ui.theme.Neutral200
import com.aiblackbox.portal.ui.theme.Neutral300
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.SolidGreen

// =============================================================================
// Provider accent colors — brand-aligned
// =============================================================================
val ClaudeAccent  = Color(0xFF4A9EFF)    // Blue (matches Portal claude-banner gradient)
val GeminiAccent  = Color(0xFF4285F4)    // Google blue
val OpenAIAccent  = Color(0xFF10A37F)    // OpenAI green
val GrokAccent    = Color(0xFFFFFFFF)    // xAI white

// =============================================================================
// ProviderBanner — aligned with Portal agent banners
//
// Portal structure (Claude example):
//   .claude-code-banner.collapsed
//     .claude-banner-collapsed (thin accent line, clickable to expand)
//       status-compact | title-compact | metrics (model|context|cost|duration) | toggle
//   .claude-code-banner.expanded
//     .claude-banner-expanded
//       Row 1: title + hint + collapse button
//       Row 2: model selector + actions (New Session, Snapshot)
//       Row 3: status + permission mode
//
// Key Portal styling:
//   - Top border: 2px gradient accent line
//   - Background: Neutral100 (#141414)
//   - Collapsed: single compact row with metrics
//   - Expanded: three rows with full controls
// =============================================================================

@Composable
fun ProviderBanner(
    providerName: String,
    isConnected: Boolean = false,
    isExpanded: Boolean = true,
    model: String = "",
    status: String = "Ready",
    // Session metrics (collapsed banner inline display)
    contextPercent: Int = 0,
    costDollars: String = "0.00",
    duration: String = "0s",
    // Permission mode (Claude only)
    permissionMode: String = "",
    onModelChange: (String) -> Unit = {},
    onNewSession: () -> Unit = {},
    onSnapshot: () -> Unit = {},
    onEndSession: () -> Unit = {},
    onToggleExpand: () -> Unit = {},
    onPermissionModeChange: () -> Unit = {},
    accentColor: Color = ClaudeAccent,
    modifier: Modifier = Modifier,
    extraContent: @Composable (() -> Unit)? = null
) {
    var expanded by remember(isExpanded) { mutableStateOf(isExpanded) }
    val view = LocalView.current

    Column(
        modifier = modifier
            .fillMaxWidth()
            .animateContentSize()
    ) {
        // -- Accent top border line (2dp gradient) --
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .height(2.dp)
                .background(accentColor.copy(alpha = 0.6f))
        )

        // -- Banner body --
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .background(Neutral100)
        ) {
            // -- Collapsed view: single compact row with metrics --
            // Matches Portal .claude-banner-collapsed / .gemini-banner-collapsed
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .clickFeedback {
                        expanded = !expanded
                        onToggleExpand()
                    }
                    .padding(horizontal = 16.dp, vertical = 10.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                // Connection dot
                Box(
                    modifier = Modifier
                        .size(6.dp)
                        .clip(CircleShape)
                        .background(if (isConnected) SolidGreen else Neutral500)
                )
                Spacer(Modifier.width(8.dp))

                // Status (Ready / Thinking / Streaming)
                Text(
                    text = status,
                    style = MaterialTheme.typography.labelSmall.copy(
                        fontWeight = FontWeight.Medium
                    ),
                    color = if (isConnected) SolidGreen else Neutral500
                )
                Spacer(Modifier.width(8.dp))

                // Provider title
                Text(
                    text = providerName,
                    style = MaterialTheme.typography.labelMedium.copy(
                        fontWeight = FontWeight.SemiBold
                    ),
                    color = accentColor
                )

                Spacer(Modifier.weight(1f))

                // -- Inline metrics (shown when session active) --
                // Matches Portal .banner-metrics
                if (isConnected && model.isNotBlank()) {
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(4.dp)
                    ) {
                        // Model name
                        Text(
                            text = model.replaceFirstChar { it.uppercase() },
                            style = MaterialTheme.typography.labelSmall,
                            color = BbxDim
                        )
                        MetricSep()
                        // Context %
                        Text(
                            text = "$contextPercent%",
                            style = MaterialTheme.typography.labelSmall,
                            color = BbxDim
                        )
                        MetricSep()
                        // Cost
                        Text(
                            text = "$$costDollars",
                            style = MaterialTheme.typography.labelSmall,
                            color = BbxDim
                        )
                        MetricSep()
                        // Duration
                        Text(
                            text = duration,
                            style = MaterialTheme.typography.labelSmall,
                            color = BbxDim
                        )
                    }
                }

                Spacer(Modifier.width(8.dp))

                // Expand/collapse arrow
                Text(
                    text = if (expanded) "\u25B2" else "\u25BC",
                    color = Neutral500,
                    style = MaterialTheme.typography.labelSmall
                )
            }

            // -- Expanded content --
            // Matches Portal .claude-banner-expanded / .gemini-banner-expanded
            AnimatedVisibility(
                visible = expanded,
                enter = expandVertically(),
                exit = shrinkVertically()
            ) {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 16.dp)
                        .padding(bottom = 12.dp),
                    verticalArrangement = Arrangement.spacedBy(10.dp)
                ) {
                    // Separator
                    Box(
                        Modifier
                            .fillMaxWidth()
                            .height(1.dp)
                            .background(GlassBorder)
                    )

                    // -- Row: Title + Hint --
                    Column {
                        Text(
                            text = providerName,
                            style = MaterialTheme.typography.titleMedium.copy(
                                fontWeight = FontWeight.SemiBold
                            ),
                            color = accentColor
                        )
                        Text(
                            text = when {
                                providerName.contains("Claude", ignoreCase = true) ->
                                    "Interactive AI coding agent with file access"
                                providerName.contains("Gemini", ignoreCase = true) ->
                                    "Interactive AI coding agent with MCP tools"
                                else -> "AI agent"
                            },
                            style = MaterialTheme.typography.labelSmall,
                            color = Neutral500
                        )
                    }

                    // -- Row: Model selector chips --
                    // Matches Portal .claude-model-selector
                    val modelOptions = when {
                        providerName.contains("Claude", ignoreCase = true) ->
                            listOf("opus" to "Opus 4.6", "sonnet" to "Sonnet 4.6", "haiku" to "Haiku 4.5")
                        else -> emptyList()
                    }

                    if (modelOptions.isNotEmpty()) {
                        Row(
                            horizontalArrangement = Arrangement.spacedBy(8.dp)
                        ) {
                            Text(
                                text = "Model:",
                                style = MaterialTheme.typography.labelSmall,
                                color = Neutral500,
                                modifier = Modifier.align(Alignment.CenterVertically)
                            )
                            modelOptions.forEach { (id, label) ->
                                val isSelected = model == id
                                Box(
                                    modifier = Modifier
                                        .clip(RoundedCornerShape(16.dp))
                                        .background(
                                            if (isSelected) accentColor.copy(alpha = 0.2f)
                                            else Neutral200
                                        )
                                        .clickFeedback {
                                            onModelChange(id)
                                        }
                                        .padding(horizontal = 12.dp, vertical = 6.dp)
                                ) {
                                    Text(
                                        text = label,
                                        style = MaterialTheme.typography.labelMedium,
                                        color = if (isSelected) accentColor else Neutral500
                                    )
                                }
                            }
                        }
                    }

                    // -- Row: Action buttons --
                    // Matches Portal .claude-banner-actions
                    Row(
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        TextButton(onClick = {
                            view.performPressFeedback()
                            onNewSession()
                        }) {
                            Text(
                                "+ New Session",
                                color = accentColor,
                                style = MaterialTheme.typography.labelMedium
                            )
                        }
                        TextButton(onClick = {
                            view.performPressFeedback()
                            onSnapshot()
                        }) {
                            Text(
                                "Snapshot",
                                color = BbxDim,
                                style = MaterialTheme.typography.labelMedium
                            )
                        }
                    }

                    // -- Row: Status + Permission mode --
                    // Matches Portal .claude-banner-status-row
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Text(
                            text = status,
                            style = MaterialTheme.typography.labelSmall,
                            color = if (isConnected) SolidGreen else Neutral500
                        )

                        // Permission mode badge (Claude only)
                        // Matches Portal .permission-mode.mode-auto / mode-normal / mode-plan
                        if (permissionMode.isNotBlank()) {
                            val modeColor = when {
                                permissionMode.contains("Auto", ignoreCase = true) -> SolidGreen
                                permissionMode.contains("Confirm", ignoreCase = true) ||
                                        permissionMode.contains("Normal", ignoreCase = true) -> Color(0xFFFFC107)
                                permissionMode.contains("Plan", ignoreCase = true) -> ClaudeAccent
                                else -> BbxAccent
                            }
                            Box(
                                modifier = Modifier
                                    .clip(RoundedCornerShape(12.dp))
                                    .background(modeColor.copy(alpha = 0.15f))
                                    .clickFeedback {
                                        onPermissionModeChange()
                                    }
                                    .padding(horizontal = 10.dp, vertical = 4.dp)
                            ) {
                                Text(
                                    text = permissionMode,
                                    style = MaterialTheme.typography.labelSmall.copy(
                                        fontWeight = FontWeight.Medium,
                                        letterSpacing = 0.5.sp
                                    ),
                                    color = modeColor
                                )
                            }
                        }
                    }

                    // Extra content slot (e.g., status line, YOLO toggle)
                    extraContent?.invoke()
                }
            }
        }
    }
}

@Composable
private fun MetricSep() {
    Text(
        text = "|",
        style = MaterialTheme.typography.labelSmall,
        color = Neutral300,
        modifier = Modifier.padding(horizontal = 2.dp)
    )
}
