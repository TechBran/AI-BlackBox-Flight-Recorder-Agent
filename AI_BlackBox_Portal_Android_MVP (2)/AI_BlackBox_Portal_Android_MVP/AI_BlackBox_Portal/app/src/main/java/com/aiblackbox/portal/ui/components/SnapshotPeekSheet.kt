package com.aiblackbox.portal.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Text
import androidx.compose.material3.rememberModalBottomSheetState
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
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.HighlightSnapshot
import com.aiblackbox.portal.ui.theme.Neutral100
import com.aiblackbox.portal.ui.theme.Neutral200
import com.aiblackbox.portal.ui.theme.Neutral300
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.RadiusMd
import com.aiblackbox.portal.ui.timeline.PeekView
import com.aiblackbox.portal.ui.timeline.TimelineViewModel
import com.aiblackbox.portal.ui.timeline.peekViewFor

/**
 * Inline bottom sheet for peeking at a snapshot's content from the chat surface.
 *
 * Reuses [TimelineViewModel.navigateToSnapshot] which fetches /fossil/snapshot/{id}
 * and exposes selectedSnapshot plus the id-tagged peek body ([TimelineViewModel.peek]).
 *
 * Nested SNAP-XXXX references inside the body are clickable via
 * [ClickableSnapshotContent]; tapping them swaps the sheet's content in place
 * (no sheet stacking) so the user can drill through related snapshots.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SnapshotPeekSheet(
    snapId: String,
    origin: String,
    onDismiss: () -> Unit,
) {
    val vm: TimelineViewModel = viewModel()
    val selected by vm.selectedSnapshot.collectAsState()
    // The body is tagged with the id it was fetched for and rendered only under that
    // id (A07-components-rest-01): never the previous snapshot's text under a new title.
    val peek by vm.peek.collectAsState()

    var currentSnapId by remember { mutableStateOf(snapId) }

    LaunchedEffect(currentSnapId) {
        if (origin.isNotBlank()) {
            vm.initialize(origin)
            vm.navigateToSnapshot(currentSnapId)
        }
    }

    ModalBottomSheet(
        onDismissRequest = onDismiss,
        containerColor = Neutral100,
        contentColor = BbxWhite,
        sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true),
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .fillMaxHeight(0.92f)
                .padding(horizontal = 20.dp, vertical = 8.dp),
        ) {
            Text(
                text = currentSnapId,
                style = MaterialTheme.typography.titleLarge.copy(fontWeight = FontWeight.Bold),
                color = HighlightSnapshot,
            )

            Spacer(Modifier.height(8.dp))

            val snap = selected
            if (snap != null && snap.snapId == currentSnapId) {
                Row(
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    if (snap.operator.isNotBlank()) MetaBadge("Operator", snap.operator, BbxDim)
                    if (snap.timestamp.isNotBlank()) MetaBadge("Time", snap.timestamp, Neutral500)
                    if (snap.type.isNotBlank()) MetaBadge("Type", snap.type, Neutral500)
                }
                Spacer(Modifier.height(12.dp))
            }

            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .weight(1f)
                    .clip(RoundedCornerShape(RadiusMd))
                    .background(Color(0xFF0A0A0A))
                    .border(1.dp, Neutral300, RoundedCornerShape(RadiusMd)),
            ) {
                when (val view = peekViewFor(peek, currentSnapId)) {
                    PeekView.Loading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                        CircularProgressIndicator(
                            color = HighlightSnapshot,
                            strokeWidth = 2.dp,
                            modifier = Modifier.size(32.dp),
                        )
                    }
                    is PeekView.Error -> Box(
                        Modifier.fillMaxSize().padding(16.dp),
                        contentAlignment = Alignment.Center,
                    ) {
                        Text(
                            text = view.message,
                            style = MaterialTheme.typography.bodySmall,
                            color = Neutral500,
                        )
                    }
                    is PeekView.Body -> Column(
                        modifier = Modifier
                            .fillMaxWidth()
                            .verticalScroll(rememberScrollState())
                            .padding(16.dp),
                    ) {
                        if (view.text.isBlank()) {
                            Text(
                                text = "No content available for this snapshot.",
                                style = MaterialTheme.typography.bodySmall.copy(
                                    fontFamily = FontFamily.Monospace,
                                    fontSize = 13.sp,
                                ),
                                color = Neutral500,
                            )
                        } else {
                            ClickableSnapshotContent(
                                text = view.text,
                                onSnapIdClick = { nestedId ->
                                    if (nestedId != currentSnapId) currentSnapId = nestedId
                                },
                            )
                        }
                    }
                }
            }

            Spacer(Modifier.height(12.dp))
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
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = "$label: ",
            style = MaterialTheme.typography.labelSmall,
            color = Neutral500,
        )
        Text(
            text = value,
            style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Medium),
            color = color,
        )
    }
}
