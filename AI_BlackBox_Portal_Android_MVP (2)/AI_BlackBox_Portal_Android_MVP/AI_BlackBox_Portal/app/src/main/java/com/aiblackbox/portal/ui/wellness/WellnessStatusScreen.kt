package com.aiblackbox.portal.ui.wellness

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aiblackbox.portal.data.wellness.HcSdkStatus
import com.aiblackbox.portal.data.wellness.WellnessCatalog
import com.aiblackbox.portal.data.wellness.WellnessRules
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.Neutral700
import java.text.DateFormat
import java.util.Date

/**
 * The status half of Settings, Health (spec §6.1): the SOURCE card (this phone, what writes
 * into Health Connect, the history window, the heart-rate origin choice) and the SYNC card
 * (last push, what is waiting, the foreground-only notice, Sync now, the interval). Facts
 * only, no health values: sources are named with counts the probe found, never readings.
 */
@Composable
fun WellnessStatusScreen(state: WellnessUiState, viewModel: WellnessViewModel) {
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        SourceCard(state, viewModel)
        SyncCard(state, viewModel)
    }
}

@Composable
private fun SourceCard(state: WellnessUiState, viewModel: WellnessViewModel) {
    HealthCard("Health source") {
        val phone = state.box?.sourceLabel ?: "This phone"
        Text("Health source: $phone", style = MaterialTheme.typography.bodyMedium, color = BbxWhite)
        HealthNote(when (state.sdk) {
            HcSdkStatus.AVAILABLE -> "Health Connect: available."
            HcSdkStatus.NEEDS_UPDATE -> "Health Connect: needs an update."
            HcSdkStatus.UNAVAILABLE -> "Health Connect: not available on this phone."
        } + " Background access: " + (if (WellnessCatalog.PERMISSION_BACKGROUND in state.granted) "granted." else "off.") +
            " Past data: " + (state.sync.earliestAvailableMs?.let { "from " + DateFormat.getDateInstance(DateFormat.MEDIUM).format(Date(it)) + "." }
                ?: if (WellnessCatalog.PERMISSION_HISTORY in state.granted) "allowed." else "last 30 days only."))
        if (state.samsungInstalled) HealthNote("Samsung Health ${state.samsungVersion ?: ""} installed.".replace("  ", " "))
        if (state.sources.isEmpty()) {
            HealthNote("No sources found yet. Once something writes into Health Connect it appears here.")
        } else {
            for (o in state.sources) {
                val label = WellnessRules.originLabel(o.label, o.deviceType, o.model)
                val kinds = o.metrics.filter { it.count14d > 0 }.joinToString(", ") { it.metric.replace('_', ' ') }
                Text(label, style = MaterialTheme.typography.bodyMedium.copy(fontWeight = FontWeight.SemiBold), color = BbxWhite)
                HealthNote(kinds.ifBlank { "nothing in the last 14 days" } +
                    (state.hrModes[o.packageName]?.takeIf { it != "unknown" && o.metrics.any { m -> m.metric == "heart_rate" } }
                        ?.let { " · heart rate ${it.replace('_', ' ')}" } ?: ""))
            }
        }
        HealthNote("Sleep score, Energy Score, stress and ECG stay in Samsung Health; BlackBox can't read them yet.", Neutral700)
        HeartRateOriginPicker(state, viewModel)
    }
}

@Composable
private fun HeartRateOriginPicker(state: WellnessUiState, viewModel: WellnessViewModel) {
    val hrOrigins = state.sources.filter { o -> o.metrics.any { it.metric == "heart_rate" && it.count14d > 0 } }
        .map { it.packageName to WellnessRules.originLabel(it.label, it.deviceType, it.model) }.distinctBy { it.first }
    if (hrOrigins.size < 2) return
    var open by remember { mutableStateOf(false) }
    val current = state.sync.hrOriginChoice
    val currentLabel = if (current == "auto") "Automatic (the busiest wearable)" else hrOrigins.firstOrNull { it.first == current }?.second ?: current
    Row(Modifier.fillMaxWidth()) {
        HealthButton("Use heart rate from: $currentLabel") { open = true }
        DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
            DropdownMenuItem(text = { Text("Automatic (the busiest wearable)") }, onClick = { open = false; viewModel.setHeartRateOrigin("auto") })
            for ((pkg, label) in hrOrigins) {
                DropdownMenuItem(text = { Text(label) }, onClick = { open = false; viewModel.setHeartRateOrigin(pkg) })
            }
        }
    }
}

@Composable
private fun SyncCard(state: WellnessUiState, viewModel: WellnessViewModel) {
    HealthCard("Sync") {
        val sync = state.sync
        HealthNote("Last push: " + if (sync.lastPushMs > 0)
            DateFormat.getDateTimeInstance(DateFormat.MEDIUM, DateFormat.SHORT).format(Date(sync.lastPushMs)) else "not yet")
        HealthNote("Waiting on this phone: ${sync.pending} batch" + (if (sync.pending == 1) "" else "es") +
            (if (sync.quarantined > 0) " · ${sync.quarantined} set aside" else ""))
        if (!sync.backfillDone && sync.backfillPagesTotal > 0) {
            HealthNote("Importing history: ${sync.backfillPagesDone} of ${sync.backfillPagesTotal} weeks")
        }
        if (WellnessCatalog.PERMISSION_BACKGROUND !in state.granted) {
            HealthNote("Syncs when you open BlackBox (background access is off).", HealthAmber)
        }
        HealthButton("Sync now", enabled = state.bound) { viewModel.syncNow() }
        var interval by remember(sync.intervalMin) { mutableFloatStateOf(sync.intervalMin.toFloat()) }
        Text("Every ${interval.toInt()} minutes", style = MaterialTheme.typography.bodySmall, color = BbxWhite)
        Slider(
            value = interval,
            onValueChange = { interval = it },
            onValueChangeFinished = { viewModel.setInterval(interval.toInt()) },
            valueRange = WellnessRules.INTERVAL_MIN.toFloat()..WellnessRules.INTERVAL_MAX.toFloat(),
            steps = (WellnessRules.INTERVAL_MAX - WellnessRules.INTERVAL_MIN) / 15 - 1,
            colors = SliderDefaults.colors(thumbColor = BbxAccent, activeTrackColor = BbxAccent),
        )
    }
}
