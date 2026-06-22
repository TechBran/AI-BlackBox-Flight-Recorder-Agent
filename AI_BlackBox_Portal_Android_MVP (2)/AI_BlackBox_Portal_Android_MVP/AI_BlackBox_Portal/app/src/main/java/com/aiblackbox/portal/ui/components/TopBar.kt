package com.aiblackbox.portal.ui.components

import androidx.compose.animation.animateColorAsState
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
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
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.GlassFloatingBubble
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.Neutral300
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.SolidGreen
import com.aiblackbox.portal.ui.theme.glassSurface

// =============================================================================
// BlackBoxTopBar — aligned with Portal .topbar
//
// Portal structure:
//   .topbar (transparent, fixed, pointer-events: none)
//     .brand (left) — "AI BB" branding
//     .operator-menu-bubble (center) — floating glass bubble
//       .operator-row-top — operator select + divider + hamburger
//       .operator-row-stats — snapshots • checkpoint • health dot
//     .timeline-btn (right) — floating glass button
//
// Key Portal styling:
//   - Topbar itself is transparent, only bubbles are visible
//   - Bubbles use rgba(34,34,34,0.95) + blur(20px) + border
//   - Everything floats over the chat content
// =============================================================================

private val BubbleShape = RoundedCornerShape(16.dp) // --radius-xl

@Composable
fun BlackBoxTopBar(
    operator: String,
    operators: List<String> = listOf("Brandon"),
    snapshotCount: Int = 0,
    checkpointTurns: Int = 0,
    isHealthy: Boolean = true,
    onMenuClick: () -> Unit = {},
    onOperatorChange: (String) -> Unit = {},
    onAddOperator: (String) -> Unit = {}
) {
    val healthColor by animateColorAsState(
        targetValue = if (isHealthy) SolidGreen else BbxAccent,
        label = "health"
    )
    var showOperatorMenu by remember { mutableStateOf(false) }
    var showAddDialog by remember { mutableStateOf(false) }
    var newOperatorName by remember { mutableStateOf("") }

    // Transparent topbar — floating pills, no background
    // Box layout so operator pill is truly centered regardless of timeline button
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .statusBarsPadding()
            .padding(horizontal = 16.dp, vertical = 12.dp)
    ) {
        // ── Center floating pill: Operator + Stats + Menu ──
        // Truly centered on screen
        Column(
            modifier = Modifier
                .align(Alignment.TopCenter)
                .glassSurface(
                    shape = BubbleShape,
                    bg = androidx.compose.ui.graphics.Color(0xFF222222), // opaque operator pill (shared token stays glass)
                    elevation = 8.dp,
                    borderOverride = androidx.compose.ui.graphics.Color.Transparent,
                ),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            // ── Row 1: Operator dropdown + divider + hamburger menu ──
            // Matches Portal .operator-row-top
            Row(
                verticalAlignment = Alignment.CenterVertically
            ) {
                // Operator label
                Text(
                    text = "Operator:",
                    style = MaterialTheme.typography.labelSmall,
                    color = Neutral500,
                    modifier = Modifier.padding(start = 12.dp)
                )

                // Operator name (tappable dropdown) — RED for visibility
                Box {
                    Text(
                        text = operator,
                        style = MaterialTheme.typography.labelMedium.copy(
                            fontWeight = FontWeight.Medium
                        ),
                        color = BbxAccent,
                        modifier = Modifier
                            .clickable { showOperatorMenu = true }
                            .padding(horizontal = 8.dp, vertical = 14.dp)
                    )
                    DropdownMenu(
                        expanded = showOperatorMenu,
                        onDismissRequest = { showOperatorMenu = false }
                    ) {
                        operators.forEach { op ->
                            DropdownMenuItem(
                                text = {
                                    Text(
                                        op,
                                        color = if (op == operator) BbxAccent else BbxWhite,
                                        fontWeight = if (op == operator) FontWeight.Bold else FontWeight.Normal
                                    )
                                },
                                onClick = {
                                    onOperatorChange(op)
                                    showOperatorMenu = false
                                }
                            )
                        }
                        // Divider + Add new operator
                        Box(
                            Modifier
                                .fillMaxWidth()
                                .padding(horizontal = 12.dp)
                                .height(1.dp)
                                .background(GlassBorder)
                        )
                        DropdownMenuItem(
                            text = {
                                Text(
                                    "+ New Operator",
                                    color = BbxAccent,
                                    fontWeight = FontWeight.Medium
                                )
                            },
                            onClick = {
                                showOperatorMenu = false
                                newOperatorName = ""
                                showAddDialog = true
                            }
                        )
                    }
                }

                // Divider (matches Portal .bubble-divider)
                Box(
                    Modifier
                        .width(1.dp)
                        .height(20.dp)
                        .background(GlassBorder)
                )

                // Hamburger menu button — RED
                Text(
                    text = "\u2630", // ☰
                    style = MaterialTheme.typography.titleMedium,
                    color = BbxAccent,
                    modifier = Modifier
                        .clickable { onMenuClick() }
                        .padding(horizontal = 14.dp, vertical = 14.dp)
                )
            }

            // ── Row 2: Stats (snapshots • checkpoint • health) ──
            // Matches Portal .operator-row-stats
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.Center,
                modifier = Modifier.padding(horizontal = 12.dp, vertical = 6.dp)
            ) {
                // Snapshot count — RED
                Text(
                    text = "\uD83D\uDCF8 $snapshotCount",
                    style = MaterialTheme.typography.labelSmall,
                    color = BbxAccent
                )

                Text(
                    text = " \u2022 ",
                    style = MaterialTheme.typography.labelSmall,
                    color = Neutral500
                )

                // Checkpoint turns — RED
                Text(
                    text = "\uD83D\uDCBE $checkpointTurns",
                    style = MaterialTheme.typography.labelSmall,
                    color = BbxAccent
                )

                Text(
                    text = " \u2022 ",
                    style = MaterialTheme.typography.labelSmall,
                    color = Neutral500
                )

                // Health indicator (matches Portal .health-dot)
                Box(
                    modifier = Modifier
                        .size(6.dp)
                        .clip(CircleShape)
                        .background(healthColor)
                )
                Spacer(Modifier.width(4.dp))
                Text(
                    text = if (isHealthy) "Connected" else "Error",
                    style = MaterialTheme.typography.labelSmall,
                    color = healthColor
                )
            }
        }
    }

    // ── Add Operator Dialog ──
    if (showAddDialog) {
        AlertDialog(
            onDismissRequest = { showAddDialog = false },
            title = { Text("New Operator", color = BbxWhite) },
            text = {
                OutlinedTextField(
                    value = newOperatorName,
                    onValueChange = { newOperatorName = it },
                    placeholder = { Text("Operator name", color = Neutral500) },
                    singleLine = true,
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedTextColor = BbxWhite,
                        unfocusedTextColor = BbxWhite,
                        focusedBorderColor = BbxAccent,
                        unfocusedBorderColor = Neutral300,
                        cursorColor = BbxAccent
                    )
                )
            },
            confirmButton = {
                TextButton(
                    onClick = {
                        val name = newOperatorName.trim()
                        if (name.isNotBlank()) {
                            onAddOperator(name)
                            showAddDialog = false
                        }
                    }
                ) {
                    Text("Create", color = BbxAccent)
                }
            },
            dismissButton = {
                TextButton(onClick = { showAddDialog = false }) {
                    Text("Cancel", color = Neutral500)
                }
            },
            containerColor = GlassFloatingBubble
        )
    }
}
