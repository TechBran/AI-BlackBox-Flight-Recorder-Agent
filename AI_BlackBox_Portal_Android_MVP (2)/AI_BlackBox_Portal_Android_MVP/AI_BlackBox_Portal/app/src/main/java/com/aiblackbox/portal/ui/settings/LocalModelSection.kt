package com.aiblackbox.portal.ui.settings

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
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.aiblackbox.portal.data.model.LocalBundle
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.Neutral200
import com.aiblackbox.portal.ui.theme.Neutral300
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.RadiusMd
import com.aiblackbox.portal.ui.theme.SolidGreen

/**
 * On-Device Model (Gemma) settings section — the Model Manager UI (Task 1.5).
 *
 * Renders the downloadable catalog with per-bundle Download / Installed states,
 * a "Recommended for your phone" badge (driven by
 * [LocalModelViewModel] → [com.aiblackbox.portal.data.local.LocalModelManager.recommendForDevice]),
 * a YOLO ⇄ Permission autonomy toggle, and an inert "Enable Accessibility" CTA
 * (Phase 4 wires it). Styling matches the rest of [SettingsSheet] — Neutral200
 * glass rows, RadiusMd corners, the project theme tokens.
 *
 * Stateless w.r.t. construction: the caller hands in a [LocalModelViewModel]
 * (built via [LocalModelViewModel.fromContext]) and this Composable just
 * observes [LocalModelViewModel.state] + dispatches actions.
 */
@Composable
fun LocalModelSection(viewModel: LocalModelViewModel) {
    val state by viewModel.state.collectAsState()

    Column(Modifier.fillMaxWidth()) {
        SectionHeaderLocal("📲 On-Device Model (Gemma)", BbxAccent)

        Text(
            "Run a Gemma model directly on this phone — works offline, no server round-trip.",
            style = MaterialTheme.typography.bodySmall,
            color = Neutral500,
            modifier = Modifier.padding(bottom = 12.dp),
        )

        // ── Catalog rows ────────────────────────────────────────────────
        if (state.catalog.isEmpty()) {
            Text(
                "No on-device models available.",
                style = MaterialTheme.typography.bodySmall,
                color = Neutral500,
                modifier = Modifier.padding(vertical = 8.dp),
            )
        } else {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                state.catalog.forEach { bundle ->
                    BundleRow(
                        bundle = bundle,
                        installed = state.isInstalled(bundle.slug),
                        recommended = bundle.slug == state.recommendedSlug,
                        active = bundle.slug == state.activeSlug,
                        progress = state.downloadProgress[bundle.slug],
                        busy = state.busySlug == bundle.slug,
                        onDownload = { viewModel.download(bundle) },
                        onSwitch = { viewModel.switchModel(bundle.slug) },
                        onDelete = { viewModel.delete(bundle.slug) },
                    )
                }
            }
        }

        Spacer(Modifier.height(16.dp))

        // ── Autonomy toggle ─────────────────────────────────────────────
        val isYolo = state.autonomyMode == AUTONOMY_YOLO
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f).padding(end = 12.dp)) {
                Text(
                    if (isYolo) "Autonomy: YOLO" else "Autonomy: Permission",
                    style = MaterialTheme.typography.bodyMedium.copy(fontWeight = FontWeight.Medium),
                    color = BbxWhite,
                )
                Text(
                    if (isYolo)
                        "Full autonomy — acts without asking."
                    else
                        "Asks before high-consequence phone actions.",
                    style = MaterialTheme.typography.labelSmall,
                    color = Neutral500,
                )
            }
            Switch(
                checked = isYolo,
                onCheckedChange = { yolo ->
                    viewModel.setAutonomy(if (yolo) AUTONOMY_YOLO else AUTONOMY_PERMISSION)
                },
                colors = SwitchDefaults.colors(
                    checkedThumbColor = BbxAccent,
                    checkedTrackColor = BbxAccent.copy(alpha = 0.3f),
                    uncheckedThumbColor = Neutral500,
                    uncheckedTrackColor = Neutral300,
                ),
            )
        }

        Spacer(Modifier.height(12.dp))

        // ── Accessibility CTA (inert — Phase 4 wires it) ─────────────────
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .clip(RoundedCornerShape(RadiusMd))
                .background(Neutral200.copy(alpha = 0.5f))
                .border(1.dp, GlassBorder, RoundedCornerShape(RadiusMd))
                .padding(12.dp),
        ) {
            Column {
                Text(
                    "Enable Accessibility",
                    style = MaterialTheme.typography.bodyMedium.copy(fontWeight = FontWeight.Medium),
                    color = Neutral500,
                )
                Text(
                    "Required for phone control (coming soon).",
                    style = MaterialTheme.typography.labelSmall,
                    color = Neutral500,
                )
            }
        }

        // ── Error ────────────────────────────────────────────────────────
        state.error?.let { err ->
            Spacer(Modifier.height(12.dp))
            Text(
                err,
                style = MaterialTheme.typography.bodySmall,
                color = BbxAccent,
                modifier = Modifier.fillMaxWidth(),
            )
        }
    }
}

/** One catalog bundle row: name + size, recommended badge, primary action. */
@Composable
private fun BundleRow(
    bundle: LocalBundle,
    installed: Boolean,
    recommended: Boolean,
    active: Boolean,
    progress: Float?,
    busy: Boolean,
    onDownload: () -> Unit,
    onSwitch: () -> Unit,
    onDelete: () -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(RadiusMd))
            .background(Neutral200)
            .border(
                1.dp,
                if (active) SolidGreen else GlassBorder,
                RoundedCornerShape(RadiusMd),
            )
            .padding(12.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        bundle.displayName.ifBlank { bundle.slug },
                        style = MaterialTheme.typography.bodyMedium.copy(fontWeight = FontWeight.Medium),
                        color = BbxWhite,
                    )
                    if (recommended) {
                        Spacer(Modifier.width(8.dp))
                        RecommendedBadge()
                    }
                }
                Text(
                    sizeLabel(bundle),
                    style = MaterialTheme.typography.labelSmall.copy(fontFamily = FontFamily.Monospace),
                    color = Neutral500,
                )
            }

            Spacer(Modifier.width(8.dp))

            // Primary action area.
            when {
                progress != null -> {
                    // Downloading — indeterminate (-1f) shows a spinner.
                    if (progress < 0f) {
                        CircularProgressIndicator(
                            color = BbxAccent,
                            strokeWidth = 2.dp,
                            modifier = Modifier.size(20.dp),
                        )
                    } else {
                        Text(
                            "${(progress * 100).toInt()}%",
                            style = MaterialTheme.typography.labelMedium,
                            color = BbxAccent,
                        )
                    }
                }

                installed -> {
                    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        if (active) {
                            PillLabel("Active", SolidGreen)
                        } else {
                            ActionPill("Switch", BbxAccent, enabled = !busy, onClick = onSwitch)
                        }
                        ActionPill("Delete", Neutral500, enabled = !busy, onClick = onDelete)
                    }
                }

                else -> {
                    ActionPill("Download", BbxAccent, enabled = !busy, onClick = onDownload)
                }
            }
        }

        // Progress bar row beneath when a determinate download is in flight.
        if (progress != null && progress >= 0f) {
            Spacer(Modifier.height(8.dp))
            LinearProgressIndicator(
                progress = { progress },
                modifier = Modifier.fillMaxWidth(),
                color = BbxAccent,
                trackColor = Neutral300,
            )
        }
    }
}

@Composable
private fun RecommendedBadge() {
    Box(
        modifier = Modifier
            .clip(RoundedCornerShape(RadiusMd))
            .background(SolidGreen.copy(alpha = 0.15f))
            .border(1.dp, SolidGreen.copy(alpha = 0.5f), RoundedCornerShape(RadiusMd))
            .padding(horizontal = 8.dp, vertical = 2.dp),
    ) {
        Text(
            "Recommended for your phone",
            style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.SemiBold, fontSize = 10.sp),
            color = SolidGreen,
        )
    }
}

@Composable
private fun ActionPill(label: String, color: androidx.compose.ui.graphics.Color, enabled: Boolean, onClick: () -> Unit) {
    Box(
        modifier = Modifier
            .clip(RoundedCornerShape(RadiusMd))
            .border(1.dp, color.copy(alpha = if (enabled) 0.6f else 0.25f), RoundedCornerShape(RadiusMd))
            .then(if (enabled) Modifier.clickable(onClick = onClick) else Modifier)
            .padding(horizontal = 12.dp, vertical = 6.dp),
    ) {
        Text(
            label,
            style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Medium),
            color = if (enabled) color else color.copy(alpha = 0.4f),
        )
    }
}

@Composable
private fun PillLabel(label: String, color: androidx.compose.ui.graphics.Color) {
    Box(
        modifier = Modifier
            .clip(RoundedCornerShape(RadiusMd))
            .background(color.copy(alpha = 0.15f))
            .padding(horizontal = 12.dp, vertical = 6.dp),
    ) {
        Text(
            label,
            style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.SemiBold),
            color = color,
        )
    }
}

/** Section header matching SettingsSheet's private SectionHeader styling. */
@Composable
private fun SectionHeaderLocal(text: String, color: androidx.compose.ui.graphics.Color) {
    Column(modifier = Modifier.padding(bottom = 8.dp)) {
        Text(
            text,
            style = MaterialTheme.typography.labelLarge.copy(
                fontWeight = FontWeight.SemiBold,
                fontSize = 14.sp,
            ),
            color = color,
        )
        Spacer(Modifier.height(6.dp))
        Box(Modifier.fillMaxWidth().height(1.dp).background(Neutral300))
    }
}

/**
 * Human-readable size: prefer the catalog's `size_bytes`; when null (pre-fetch),
 * fall back to the min-RAM hint so the row is never blank.
 */
internal fun sizeLabel(bundle: LocalBundle): String {
    val bytes = bundle.sizeBytes
    return if (bytes != null && bytes > 0L) {
        humanBytes(bytes)
    } else if (bundle.minRamGb > 0.0) {
        "needs ${trimZero(bundle.minRamGb)} GB RAM"
    } else {
        "—" // em dash
    }
}

/** Format a byte count as KB/MB/GB with one decimal (binary units). */
internal fun humanBytes(bytes: Long): String {
    if (bytes < 1024) return "$bytes B"
    val units = arrayOf("KB", "MB", "GB", "TB")
    var value = bytes.toDouble() / 1024.0
    var idx = 0
    while (value >= 1024.0 && idx < units.size - 1) {
        value /= 1024.0
        idx++
    }
    return "${trimZero((value * 10).toLong() / 10.0)} ${units[idx]}"
}

private fun trimZero(v: Double): String =
    if (v == v.toLong().toDouble()) v.toLong().toString() else v.toString()
