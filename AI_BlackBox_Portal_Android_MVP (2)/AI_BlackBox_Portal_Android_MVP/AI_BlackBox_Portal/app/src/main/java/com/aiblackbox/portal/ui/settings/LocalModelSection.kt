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
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.Neutral200
import com.aiblackbox.portal.ui.theme.Neutral300
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.RadiusMd
import com.aiblackbox.portal.ui.theme.SolidGreen

/**
 * On-Device Model (Gemma) settings section — the Edge-Gallery-style picker /
 * Model Manager UI (Task 1.5 + W5).
 *
 * Renders the merged picker rows from [LocalModelUiState.rows] (the pure
 * [modelRowsFrom] reducer): per-model Download → progress / Use (set active) /
 * Delete / Retry actions, a Recommended badge + the per-model context note (W6;
 * the catalog's `recommended` flag both badges AND sorts the row first), the
 * active on-device model clearly marked, plus a YOLO ⇄ Permission autonomy
 * toggle and an OPTIONAL "Advanced screen control" affordance (R2: a11y is NOT
 * required for the on-device model — only for the gesture read-screen/tap layer).
 * Styling matches the rest of [SettingsSheet] — Neutral200 glass rows, RadiusMd
 * corners, the project tokens.
 *
 * Stateless w.r.t. construction: the caller hands in a [LocalModelViewModel]
 * (built via [LocalModelViewModel.fromContext]) and this Composable just
 * observes [LocalModelViewModel.state] + dispatches actions.
 *
 * The OPTIONAL "Advanced screen control" affordance (Phase 4.1, reframed in R2)
 * is wired by the screen: it passes [accessibilityEnabled] (read from the live
 * `Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES` setting → the pure
 * [com.aiblackbox.portal.overlay.isAccessibilityServiceEnabled] helper) and an
 * [onEnableAccessibility] lambda that deep-links to system Accessibility
 * settings. Both default to inert so previews/older callers still compile. The
 * capability is unchanged — a11y still drives the gesture read-screen/tap layer;
 * only the framing is secondary (intents/chat need NO accessibility).
 */
@Composable
fun LocalModelSection(
    viewModel: LocalModelViewModel,
    accessibilityEnabled: Boolean = false,
    onEnableAccessibility: () -> Unit = {},
) {
    val state by viewModel.state.collectAsState()

    Column(Modifier.fillMaxWidth()) {
        SectionHeaderLocal("📲 On-Device Model (Gemma)", BbxAccent)

        Text(
            "Run a Gemma model directly on this phone — works offline, no server round-trip.",
            style = MaterialTheme.typography.bodySmall,
            color = Neutral500,
            modifier = Modifier.padding(bottom = 12.dp),
        )

        // ── Picker rows (Task W5.3: recommended-first, state-driven) ──────
        val rows = state.rows
        if (rows.isEmpty()) {
            // GENUINELY zero installed (R2): only here do we say "none available".
            // The raw catalog error (if any) is shown by the error block below.
            Text(
                "No on-device models available.",
                style = MaterialTheme.typography.bodySmall,
                color = Neutral500,
                modifier = Modifier.padding(vertical = 8.dp),
            )
        } else {
            // R2: installed models present. If the catalog is unreachable (refresh
            // set an error) surface a CLEAR secondary note that the installed
            // models still work — never the alarming "no models available".
            if (state.error != null) {
                Text(
                    "Model catalog unavailable (server not reachable) — " +
                        "your installed models still work.",
                    style = MaterialTheme.typography.bodySmall,
                    color = Neutral500,
                    modifier = Modifier.padding(bottom = 8.dp),
                )
            }
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                rows.forEach { row ->
                    ModelRowCard(
                        row = row,
                        busy = state.busySlug == row.slug,
                        onDownload = { viewModel.download(row.bundle) },
                        onRetry = { viewModel.retry(row.bundle) },
                        onSwitch = { viewModel.switchModel(row.slug) },
                        onDelete = { viewModel.delete(row.slug) },
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

        // ── Advanced screen control (OPTIONAL) ───────────────────────────
        // R2: a11y is NO LONGER required for the on-device model. Intents/chat
        // (flashlight, maps, calls, dice, …) work WITHOUT accessibility — it is
        // only needed for the OPTIONAL gesture layer (read the screen / tap UI in
        // OTHER apps). So this is reframed as a clearly-secondary, optional
        // affordance, never a prominent "required" CTA. Tapping still deep-links
        // to the system Accessibility settings list (Android does not reliably
        // support deep-linking to a single service's toggle); the capability is
        // unchanged, only the framing.
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .clip(RoundedCornerShape(RadiusMd))
                .background(
                    if (accessibilityEnabled) SolidGreen.copy(alpha = 0.10f)
                    else Neutral200.copy(alpha = 0.3f),
                )
                .border(
                    1.dp,
                    if (accessibilityEnabled) SolidGreen.copy(alpha = 0.4f) else GlassBorder,
                    RoundedCornerShape(RadiusMd),
                )
                .clickable(onClick = onEnableAccessibility)
                .padding(12.dp),
        ) {
            Column {
                Text(
                    if (accessibilityEnabled)
                        "Advanced screen control: on"
                    else
                        "Advanced screen control (optional)",
                    // Secondary weight + muted color: this is no longer a primary CTA.
                    style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Medium),
                    color = if (accessibilityEnabled) SolidGreen else Neutral500,
                )
                Text(
                    if (accessibilityEnabled)
                        "The model can read the screen and tap UI in other apps. " +
                            "Tap to manage in system settings."
                    else
                        "Only needed if you want the model to read the screen and tap UI " +
                            "in other apps. Flashlight, maps, calls and other actions work " +
                            "without it. Tap to enable in system settings.",
                    style = MaterialTheme.typography.labelSmall,
                    color = Neutral500,
                )
            }
        }

        // ── Error ────────────────────────────────────────────────────────
        // When models ARE listed, the catalog error is already softened into the
        // "catalog unavailable — your installed models still work" note above, so
        // we don't ALSO show the raw error here (alarming + redundant). With ZERO
        // models listed there is no note, so the raw error (e.g. "Couldn't load
        // model catalog: HTTP 404") still shows here.
        if (state.rows.isEmpty()) {
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
}

/**
 * One picker row (Task W5.3): model name + size, a Recommended badge + the
 * context note, and a state-appropriate action -- Download / Use / Delete /
 * Retry. The active on-device model is clearly marked (Active pill + green
 * border). Renders directly off a [ModelRow] from the pure [modelRowsFrom]
 * reducer, so all state precedence lives in one tested place.
 */
@Composable
private fun ModelRowCard(
    row: ModelRow,
    busy: Boolean,
    onDownload: () -> Unit,
    onRetry: () -> Unit,
    onSwitch: () -> Unit,
    onDelete: () -> Unit,
) {
    val bundle = row.bundle
    val active = (row.state as? ModelRowState.Installed)?.active == true
    val failed = row.state is ModelRowState.Failed
    val downloading = row.state as? ModelRowState.Downloading

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(RadiusMd))
            .background(Neutral200)
            .border(
                1.dp,
                when {
                    active -> SolidGreen
                    failed -> BbxAccent.copy(alpha = 0.5f)
                    else -> GlassBorder
                },
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
                    if (row.recommended) {
                        Spacer(Modifier.width(8.dp))
                        RecommendedBadge()
                    }
                }
                Text(
                    sizeLabel(bundle),
                    style = MaterialTheme.typography.labelSmall.copy(fontFamily = FontFamily.Monospace),
                    color = Neutral500,
                )
                // Per-model context note (W6): "Recommended -- ..." / "Experimental -- ...".
                row.contextNote?.let { note ->
                    Text(
                        note,
                        style = MaterialTheme.typography.labelSmall,
                        color = if (row.recommended) SolidGreen else Neutral500,
                    )
                }
            }

            Spacer(Modifier.width(8.dp))

            // Primary action area -- one branch per row state.
            when {
                downloading != null -> {
                    // Indeterminate (-1f) shows a spinner; else the percent.
                    if (downloading.progress < 0f) {
                        CircularProgressIndicator(
                            color = BbxAccent,
                            strokeWidth = 2.dp,
                            modifier = Modifier.size(20.dp),
                        )
                    } else {
                        Text(
                            "${fractionToPercent(downloading.progress)}%",
                            style = MaterialTheme.typography.labelMedium,
                            color = BbxAccent,
                        )
                    }
                }

                row.state is ModelRowState.Installed -> {
                    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        if (active) {
                            PillLabel("Active", SolidGreen)
                        } else {
                            ActionPill("Use", BbxAccent, enabled = !busy, onClick = onSwitch)
                        }
                        ActionPill("Delete", Neutral500, enabled = !busy, onClick = onDelete)
                    }
                }

                failed -> {
                    ActionPill("Retry", BbxAccent, enabled = !busy, onClick = onRetry)
                }

                else -> {
                    ActionPill("Download", BbxAccent, enabled = !busy, onClick = onDownload)
                }
            }
        }

        // Progress bar beneath when a determinate download is in flight.
        if (downloading != null && downloading.progress >= 0f) {
            Spacer(Modifier.height(8.dp))
            LinearProgressIndicator(
                progress = { downloading.progress },
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
