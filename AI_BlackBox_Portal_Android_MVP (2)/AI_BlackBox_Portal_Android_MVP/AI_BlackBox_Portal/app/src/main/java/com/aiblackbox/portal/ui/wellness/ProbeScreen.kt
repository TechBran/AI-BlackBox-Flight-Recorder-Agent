package com.aiblackbox.portal.ui.wellness

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.sp
import androidx.health.connect.client.PermissionController
import com.aiblackbox.portal.data.wellness.WellnessCatalog
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.Neutral700

/**
 * The M7.1 probe (spec §2.4 and M7.1 scope 2), behind the debug flag: it runs the device tests
 * T-1 to T-11 as far as they can run on the phone, SENDS NOTHING to the box, writes the JSON
 * report to the app's files dir and offers it to the share sheet. The report holds statuses,
 * counts, dates and gaps, never a health value.
 */
@Composable
fun ProbeScreen(state: WellnessUiState, viewModel: WellnessViewModel, onClose: () -> Unit) {
    val context = LocalContext.current
    val launcher = rememberLauncherForActivityResult(
        remember { PermissionController.createRequestPermissionResultContract() },
    ) { granted -> viewModel.onPermissionsResult(granted) }
    Column {
        HealthCard("Health probe (M7.1)") {
            HealthNote("Runs the phone side of device tests T-1 to T-11. Nothing is sent to the box; the report " +
                "is saved on this phone and you choose where to share it. Counts and dates only.")
            Row {
                HealthButton("Ask Health Connect for everything") {
                    launcher.launch(WellnessCatalog.ALL_READ_PERMISSIONS + WellnessCatalog.PERMISSION_HISTORY +
                        WellnessCatalog.PERMISSION_BACKGROUND)
                }
            }
            Row {
                HealthButton(if (state.busy) "Running…" else "Run the probe", enabled = !state.busy) { viewModel.runProbe() }
                state.probeReport?.let { report ->
                    HealthButton("Share") { HealthIntents.shareText(context, "BlackBox health probe", report) }
                }
                HealthButton("Close", color = Neutral700) { onClose() }
            }
            state.probeFile?.let { HealthNote("Saved: $it", Neutral700) }
            state.probeReport?.let {
                Text(it, style = MaterialTheme.typography.bodySmall.copy(fontFamily = FontFamily.Monospace, fontSize = 11.sp),
                    color = BbxWhite)
            }
        }
    }
}
