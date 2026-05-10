package com.aiblackbox.portal.ui.components

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.aiblackbox.portal.data.model.Provenance
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.Neutral600
import com.aiblackbox.portal.ui.theme.RadiusMd

// =============================================================================
// ContextProvenance — collapsible chip row showing retrieval provenance
//
// Collapsed: chevron + "${totalCount()} context snapshots" label
// Expanded:  four labeled sections (Recent / Keyword / Semantic / Checkpoint),
//            each rendering compact pill chips of SNAP-IDs.
//
// Uses theme tokens: Neutral500 (chevron/label), Neutral600 (section label),
// BbxDim (chip text), RadiusMd (chip radius), GlassBorder (chip border).
// =============================================================================
@Composable
fun ContextProvenance(
    provenance: Provenance,
    expanded: Boolean,
    onToggle: () -> Unit,
    modifier: Modifier = Modifier,
    onSnapshotClick: ((String) -> Unit)? = null,
) {
    if (provenance.isEmpty()) return
    Column(modifier = modifier.testTag("context-provenance-root")) {
        Row(
            modifier = Modifier
                .clickable { onToggle() }
                .semantics(mergeDescendants = true) {
                    contentDescription = if (expanded)
                        "Hide context snapshots, ${provenance.totalCount()} total"
                    else
                        "Show context snapshots, ${provenance.totalCount()} total"
                    role = Role.Button
                }
                .heightIn(min = 32.dp)
                .padding(vertical = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = if (expanded) "▼" else "▶",
                style = MaterialTheme.typography.labelSmall,
                color = Neutral500,
            )
            Spacer(Modifier.width(4.dp))
            Text(
                text = "${provenance.totalCount()} context snapshots",
                style = MaterialTheme.typography.labelSmall,
                color = Neutral500,
            )
        }
        AnimatedVisibility(visible = expanded) {
            Column(
                verticalArrangement = Arrangement.spacedBy(6.dp),
                modifier = Modifier.padding(top = 4.dp),
            ) {
                ProvenanceSection("Recent", provenance.recent, onSnapshotClick)
                ProvenanceSection("Keyword", provenance.keyword, onSnapshotClick)
                ProvenanceSection("Semantic", provenance.semantic, onSnapshotClick)
                ProvenanceSection("Checkpoint", provenance.checkpoint, onSnapshotClick)
            }
        }
    }
}

@Composable
private fun ProvenanceSection(
    label: String,
    ids: List<String>,
    onSnapshotClick: ((String) -> Unit)? = null,
) {
    if (ids.isEmpty()) return
    Column {
        Text(
            text = label,
            style = MaterialTheme.typography.labelSmall,
            color = Neutral600,
            fontWeight = FontWeight.Bold,
        )
        FlowRow(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
            ids.forEach { id ->
                val fullId = if (id.startsWith("SNAP-", ignoreCase = true)) id else "SNAP-$id"
                Box(
                    Modifier
                        .background(Color(0x14FFFFFF), RoundedCornerShape(RadiusMd))
                        .border(1.dp, GlassBorder, RoundedCornerShape(RadiusMd))
                        .let { base ->
                            if (onSnapshotClick != null) {
                                base.clickable { onSnapshotClick(fullId) }
                            } else {
                                base
                            }
                        }
                        .padding(horizontal = 6.dp, vertical = 2.dp),
                ) {
                    Text(
                        text = id.removePrefix("SNAP-"),
                        style = MaterialTheme.typography.labelSmall.copy(fontSize = 10.sp),
                        color = BbxDim,
                    )
                }
            }
        }
    }
}
