package com.aiblackbox.portal.ui.timeline

import android.view.HapticFeedbackConstants
import androidx.compose.animation.animateColorAsState
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aiblackbox.portal.data.model.SnapshotResult
import com.aiblackbox.portal.ui.components.ClickableSnapshotContent
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.GlassFloatingBubble
import com.aiblackbox.portal.ui.theme.HighlightSnapshot
import com.aiblackbox.portal.ui.theme.Neutral100
import com.aiblackbox.portal.ui.theme.Neutral200
import com.aiblackbox.portal.ui.theme.Neutral300
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.RadiusMd
import com.aiblackbox.portal.ui.theme.RadiusLg
import com.aiblackbox.portal.ui.theme.SolidGreen
import com.aiblackbox.portal.ui.theme.glassSurface

// =============================================================================
// TimelineScreen — aligned with Portal timeline-browser.js + _timeline.css
//
// Portal structure:
//   Search bar + Operator filter dropdown
//   Timeline list (.timeline-item) with hover: translateX(4px) + accent border
//   Snapshot popup (.snapshot-popup) with monospace content
//   Result count display
// =============================================================================

@Composable
fun TimelineScreen(
    origin: String,
    operator: String = "",
    operators: List<String> = listOf("Brandon"),
    modifier: Modifier = Modifier,
    viewModel: TimelineViewModel = viewModel()
) {
    val view = LocalView.current
    var searchQuery by remember { mutableStateOf("") }
    val results by viewModel.results.collectAsState()
    val isSearching by viewModel.isSearching.collectAsState()
    val selectedSnapshot by viewModel.selectedSnapshot.collectAsState()

    // Operator filter
    var selectedOperator by remember { mutableStateOf("") } // "" = all
    var showOperatorFilter by remember { mutableStateOf(false) }

    // Initialize and start auto-polling for live updates
    LaunchedEffect(origin, operator) {
        if (origin.isNotBlank()) {
            viewModel.initialize(origin)
            viewModel.loadRecent(operator)
            viewModel.startPolling(operator)
        }
    }
    // Stop polling when screen leaves composition
    androidx.compose.runtime.DisposableEffect(Unit) {
        onDispose { viewModel.stopPolling() }
    }

    Column(modifier = modifier.fillMaxSize().padding(start = 16.dp, end = 16.dp, bottom = 16.dp, top = 100.dp)) {
        // ── Header ──
        Text(
            "\uD83D\uDCF8 Timeline",
            style = MaterialTheme.typography.headlineMedium.copy(fontWeight = FontWeight.Bold),
            color = BbxWhite
        )
        Spacer(Modifier.height(12.dp))

        // ── Search bar + Operator filter ──
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically
        ) {
            // Search input (glass surface)
            Box(
                modifier = Modifier
                    .weight(1f)
                    .glassSurface(shape = RoundedCornerShape(20.dp), bg = Neutral100)
                    .padding(horizontal = 16.dp, vertical = 10.dp)
            ) {
                if (searchQuery.isEmpty()) {
                    Text(
                        "Search snapshots...",
                        color = Neutral500,
                        style = MaterialTheme.typography.bodyLarge
                    )
                }
                BasicTextField(
                    value = searchQuery,
                    onValueChange = { searchQuery = it },
                    modifier = Modifier.fillMaxWidth(),
                    textStyle = MaterialTheme.typography.bodyLarge.copy(color = BbxWhite),
                    cursorBrush = SolidColor(BbxAccent),
                    singleLine = true
                )
            }

            Spacer(Modifier.width(8.dp))

            // Search button with haptic
            Box(
                modifier = Modifier
                    .size(40.dp)
                    .clip(CircleShape)
                    .background(BbxAccent)
                    .clickable {
                        view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                        if (searchQuery.isNotBlank()) {
                            viewModel.search(searchQuery, selectedOperator.ifBlank { null })
                        } else {
                            viewModel.loadRecent(selectedOperator.ifBlank { operator })
                        }
                    },
                contentAlignment = Alignment.Center
            ) {
                Text("\uD83D\uDD0D", fontSize = 18.sp)
            }

            Spacer(Modifier.width(8.dp))

            // Operator filter dropdown
            Box {
                Box(
                    modifier = Modifier
                        .clip(RoundedCornerShape(RadiusMd))
                        .background(Neutral200)
                        .clickable {
                            view.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
                            showOperatorFilter = true
                        }
                        .padding(horizontal = 10.dp, vertical = 10.dp)
                ) {
                    Text(
                        text = selectedOperator.ifBlank { "All" },
                        style = MaterialTheme.typography.labelSmall,
                        color = if (selectedOperator.isNotBlank()) BbxAccent else Neutral500
                    )
                }
                DropdownMenu(
                    expanded = showOperatorFilter,
                    onDismissRequest = { showOperatorFilter = false }
                ) {
                    DropdownMenuItem(
                        text = { Text("All Operators", color = if (selectedOperator.isBlank()) BbxAccent else BbxWhite) },
                        onClick = {
                            selectedOperator = ""
                            showOperatorFilter = false
                            viewModel.loadRecent(operator)
                        }
                    )
                    operators.forEach { op ->
                        DropdownMenuItem(
                            text = { Text(op, color = if (selectedOperator == op) BbxAccent else BbxWhite) },
                            onClick = {
                                selectedOperator = op
                                showOperatorFilter = false
                                viewModel.loadRecent(op)
                            }
                        )
                    }
                }
            }
        }

        Spacer(Modifier.height(12.dp))

        // ── Status bar: result count + loading ──
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text(
                text = if (results.isNotEmpty()) "${results.size} snapshots" else "",
                style = MaterialTheme.typography.labelSmall,
                color = Neutral500
            )
            if (isSearching) {
                CircularProgressIndicator(color = BbxAccent, modifier = Modifier.size(16.dp), strokeWidth = 2.dp)
            }
        }

        Spacer(Modifier.height(8.dp))

        // ── Detail view or list ──
        val fullContent by viewModel.fullContent.collectAsState()
        val isLoadingContent by viewModel.isLoadingContent.collectAsState()

        if (selectedSnapshot != null) {
            SnapshotDetail(
                snapshot = selectedSnapshot!!,
                fullContent = fullContent,
                isLoading = isLoadingContent,
                onBack = {
                    view.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
                    viewModel.selectSnapshot(null)
                },
                onSnapIdClick = { snapId ->
                    view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                    viewModel.navigateToSnapshot(snapId)
                }
            )
        } else {
            if (results.isEmpty() && !isSearching) {
                Box(
                    Modifier.fillMaxSize(),
                    contentAlignment = Alignment.Center
                ) {
                    Text("No snapshots found", color = Neutral500, style = MaterialTheme.typography.bodyMedium)
                }
            } else {
                LazyColumn(
                    modifier = Modifier.fillMaxSize(),
                    verticalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    items(results, key = { it.snapId }) { snap ->
                        SnapshotCard(snap) {
                            view.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
                            viewModel.selectSnapshot(snap)
                        }
                    }
                }
            }
        }
    }
}

// =============================================================================
// SnapshotCard — matches Portal .timeline-item
// Hover: translateX(4px) + accent border, header with ID + operator badge
// =============================================================================
@Composable
private fun SnapshotCard(snapshot: SnapshotResult, onClick: () -> Unit) {
    val interactionSource = remember { MutableInteractionSource() }
    val isPressed by interactionSource.collectIsPressedAsState()

    // Animate press state (matches Portal hover: translateX 4px + accent border)
    val borderColor by animateColorAsState(
        targetValue = if (isPressed) BbxAccent.copy(alpha = 0.5f) else GlassBorder,
        label = "border"
    )
    val offsetX = if (isPressed) 4.dp else 0.dp

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .offset(x = offsetX)
            .glassSurface(shape = RoundedCornerShape(RadiusMd), bg = Neutral100)
            .border(1.dp, borderColor, RoundedCornerShape(RadiusMd))
            .clickable(interactionSource = interactionSource, indication = null, onClick = onClick)
            .padding(14.dp)
    ) {
        // Header: Snap ID + Operator badge
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            // Snap ID (accent purple, bold — matches Portal .timeline-item-id)
            Text(
                snapshot.snapId,
                style = MaterialTheme.typography.labelMedium.copy(
                    fontWeight = FontWeight.Bold,
                    fontSize = 14.sp
                ),
                color = HighlightSnapshot
            )

            // Operator badge (matches Portal .timeline-item-operator)
            if (snapshot.operator.isNotBlank()) {
                Box(
                    modifier = Modifier
                        .clip(RoundedCornerShape(8.dp))
                        .background(Neutral200)
                        .padding(horizontal = 8.dp, vertical = 2.dp)
                ) {
                    Text(
                        snapshot.operator,
                        style = MaterialTheme.typography.labelSmall,
                        color = BbxDim
                    )
                }
            }
        }

        Spacer(Modifier.height(6.dp))

        // Preview text (single line truncated — matches Portal .timeline-item-preview)
        Text(
            text = snapshot.snippet.take(200),
            style = MaterialTheme.typography.bodySmall,
            color = Neutral500,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis
        )

        Spacer(Modifier.height(6.dp))

        // Meta row: timestamp + similarity
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Text(
                snapshot.timestamp,
                style = MaterialTheme.typography.labelSmall,
                color = Neutral500
            )
            if (snapshot.similarity > 0f) {
                Text(
                    "${(snapshot.similarity * 100).toInt()}% match",
                    style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Medium),
                    color = SolidGreen
                )
            }
        }
    }
}

// =============================================================================
// SnapshotDetail — matches Portal .snapshot-popup
// Full content display, monospace, scrollable, clickable SNAP-IDs
// =============================================================================
@Composable
private fun SnapshotDetail(
    snapshot: SnapshotResult,
    fullContent: String?,
    isLoading: Boolean,
    onBack: () -> Unit,
    onSnapIdClick: (String) -> Unit
) {
    Column(modifier = Modifier.fillMaxSize()) {
        // Back button (styled, haptic)
        Row(
            modifier = Modifier
                .clip(RoundedCornerShape(RadiusMd))
                .background(Neutral200)
                .clickable(onClick = onBack)
                .padding(horizontal = 14.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text("\u2190", color = BbxAccent, fontSize = 16.sp)
            Spacer(Modifier.width(6.dp))
            Text("Back to list", color = BbxAccent, style = MaterialTheme.typography.labelMedium)
        }

        Spacer(Modifier.height(12.dp))

        // Snap ID (large, purple)
        Text(
            snapshot.snapId,
            style = MaterialTheme.typography.titleLarge.copy(fontWeight = FontWeight.Bold),
            color = HighlightSnapshot
        )

        Spacer(Modifier.height(8.dp))

        // Meta row with badges
        Row(
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            modifier = Modifier.fillMaxWidth()
        ) {
            MetaBadge(label = "Operator", value = snapshot.operator, color = BbxDim)
            MetaBadge(label = "Time", value = snapshot.timestamp, color = Neutral500)
            if (snapshot.type.isNotBlank()) {
                MetaBadge(label = "Type", value = snapshot.type, color = Neutral500)
            }
        }

        Spacer(Modifier.height(12.dp))

        // Full content (monospace, scrollable — matches Portal .snapshot-popup-body)
        Box(
            modifier = Modifier
                .fillMaxSize()
                .clip(RoundedCornerShape(RadiusMd))
                .background(Color(0xFF0A0A0A))
                .border(1.dp, Neutral300, RoundedCornerShape(RadiusMd))
        ) {
            if (isLoading && fullContent == null) {
                Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator(color = BbxAccent, modifier = Modifier.size(24.dp))
                }
            } else {
                val contentText = fullContent ?: snapshot.snippet

                Column(
                    modifier = Modifier
                        .fillMaxSize()
                        .verticalScroll(rememberScrollState())
                        .padding(16.dp)
                ) {
                    // Build annotated string with clickable SNAP-IDs
                    ClickableSnapshotContent(
                        text = contentText,
                        onSnapIdClick = onSnapIdClick
                    )
                }
            }
        }
    }
}


@Composable
private fun MetaBadge(label: String, value: String, color: Color) {
    Row(
        modifier = Modifier
            .clip(RoundedCornerShape(8.dp))
            .background(Neutral200)
            .padding(horizontal = 8.dp, vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Text(
            "$label: ",
            style = MaterialTheme.typography.labelSmall,
            color = Neutral500
        )
        Text(
            value,
            style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Medium),
            color = color
        )
    }
}
