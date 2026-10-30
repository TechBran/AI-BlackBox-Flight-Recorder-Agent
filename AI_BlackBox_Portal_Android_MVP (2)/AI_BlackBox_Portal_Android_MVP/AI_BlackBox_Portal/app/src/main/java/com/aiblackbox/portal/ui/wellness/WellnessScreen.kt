package com.aiblackbox.portal.ui.wellness

import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.health.connect.client.PermissionController
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aiblackbox.portal.BuildConfig
import com.aiblackbox.portal.data.wellness.WellnessCatalog
import com.aiblackbox.portal.data.wellness.WellnessCategory
import com.aiblackbox.portal.data.wellness.WellnessConsent
import com.aiblackbox.portal.data.wellness.WellnessRules
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxBlack
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.Neutral300
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.Neutral700

/**
 * Settings, Health (spec §6.1): the status chip, the source and sync cards, trouble cards
 * with their fixes, what BlackBox may use (Off / Ask / Proactive), where it may go ("Only AI
 * on my network", providers, local-only per kind), the health instinct prompt, where you can
 * ask, the briefing placeholder (M7.3), who saw my health data, and Pause / Disconnect /
 * Stop & erase. Not set up: the setup flow. The Probe exists only in debug builds and in a
 * build made with `-PwellnessProbe` (the M7.1 device walk).
 */
@Composable
fun WellnessScreen(viewModel: WellnessViewModel = viewModel()) {
    val state by viewModel.state.collectAsState()
    val context = LocalContext.current
    var probeOpen by rememberSaveable { mutableStateOf(false) }

    LaunchedEffect(Unit) { viewModel.refresh() }
    LaunchedEffect(state.message) {
        state.message?.let {
            Toast.makeText(context, it, Toast.LENGTH_LONG).show()
            viewModel.clearMessage()
        }
    }

    Column(
        Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 16.dp, vertical = 12.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                "Health",
                style = MaterialTheme.typography.headlineSmall.copy(fontWeight = FontWeight.Bold),
                color = BbxWhite,
                modifier = Modifier.weight(1f),
            )
            StatusChipView(state.chip)
        }
        when {
            probeOpen -> ProbeScreen(state, viewModel, onClose = { probeOpen = false })
            state.setupOpen -> WellnessSetupFlow(state, viewModel, onClose = { viewModel.closeSetup() })
            state.loading -> HealthNote("Loading…")
            !state.bound -> NotSetUp(state, onSetUp = { viewModel.startSetup() })
            else -> Enrolled(state, viewModel)
        }
        if (!probeOpen && (BuildConfig.DEBUG || BuildConfig.WELLNESS_PROBE)) {
            HealthButton("Health probe (M7.1 device tests)", color = Neutral700) { probeOpen = true }
        }
        Spacer(Modifier.height(24.dp))
    }
}

@Composable
private fun NotSetUp(state: WellnessUiState, onSetUp: () -> Unit) {
    val context = LocalContext.current
    HealthCard("Not set up") {
        HealthNote(
            if (state.box?.state == WellnessRules.EnrollmentState.AWAITING_PHONE)
                "Finish setting up Health on this phone. It reads what your watch, ring or cuff writes into Health Connect " +
                    "and keeps it in your own BlackBox, for you only."
            else "Use this phone as your health source. BlackBox reads, read-only, what your watch, ring, cuff or scale " +
                "writes into Health Connect and keeps it in your own BlackBox, for you only."
        )
        if (state.box?.enrolled == true) {
            HealthNote("You already have a health source${state.box.sourceLabel?.let { ": $it" } ?: ""}. Setting up here replaces it.")
        }
        state.troubles.forEach { TroubleCardView(it) { fix -> runFix(context, fix) } }
        HealthButton("Set up Health on this phone", enabled = state.operator.isNotBlank()) { onSetUp() }
        if (state.operator.isBlank()) HealthNote("Pick your operator in Settings first.")
        HealthButton("Privacy policy", color = Neutral700) { HealthIntents.openPrivacyPolicy(context) }
    }
}

private fun runFix(context: android.content.Context, fix: WellnessRules.TroubleFix, viewModel: WellnessViewModel? = null) {
    when (fix) {
        WellnessRules.TroubleFix.OPEN_HEALTH_CONNECT -> HealthIntents.openHealthConnect(context)
        WellnessRules.TroubleFix.UPDATE_HEALTH_CONNECT -> HealthIntents.getHealthConnect(context)
        WellnessRules.TroubleFix.OPEN_SAMSUNG_HEALTH -> HealthIntents.openSamsungHealth(context)
        WellnessRules.TroubleFix.OPEN_BATTERY_SETTINGS -> HealthIntents.openBatterySettings(context)
        WellnessRules.TroubleFix.SYNC_NOW -> viewModel?.syncNow()
        WellnessRules.TroubleFix.RECONNECT -> viewModel?.startSetup()
        WellnessRules.TroubleFix.NONE -> Unit
    }
}

@Composable
private fun Enrolled(state: WellnessUiState, viewModel: WellnessViewModel) {
    val context = LocalContext.current
    val settings = state.settings
    val launcher = rememberLauncherForActivityResult(
        remember { PermissionController.createRequestPermissionResultContract() },
    ) { granted -> viewModel.onPermissionsResult(granted) }

    WellnessStatusScreen(state, viewModel)

    if (state.troubles.isNotEmpty()) {
        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            state.troubles.forEach { TroubleCardView(it) { fix -> runFix(context, fix, viewModel) } }
        }
    }

    if (WellnessRules.showReplaceApproval(state.bound, state.box?.credentialed == true, state.box?.replacePending)) {
        HealthCard("Move Health to another phone?") {
            HealthNote("Another phone asked to become your health source. Approving moves your stored history and " +
                "settings to it, and this phone stops syncing health data.", HealthAmber)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                HealthButton("Approve the new phone") { viewModel.answerReplacement(approve = true) }
                HealthButton("Keep this phone", color = Neutral700) { viewModel.answerReplacement(approve = false) }
            }
        }
    }

    HealthCard("What BlackBox may use") {
        if (state.box?.credentialed != true) HealthNote("Couldn't read your settings from the box; showing defaults.", HealthAmber)
        for (cat in WellnessCategory.entries) {
            Text(cat.label, style = MaterialTheme.typography.bodyMedium.copy(fontWeight = FontWeight.SemiBold), color = BbxWhite)
            val consent = settings.categories[cat] ?: WellnessConsent.OFF
            ConsentSelector(consent) { viewModel.setConsent(cat, it) }
            val needed = WellnessCatalog.readPermissionsFor(listOf(cat))
            if (consent.isOn && needed.none { it in state.granted }) {
                HealthButton("Allow ${cat.label.lowercase()} in Health Connect") { launcher.launch(needed.toSet()) }
            }
            Spacer(Modifier.height(6.dp))
        }
    }

    HealthCard("Where it may go") {
        HealthSwitchRow("Only AI on my network", settings.providersAllowed.isEmpty(),
            note = "AI on your own network is always allowed. Every cloud AI needs its own tick.") { viewModel.setOnlyLocal(it) }
        for (p in state.providers) {
            HealthSwitchRow(p.label, p.id in settings.providersAllowed, note = p.note) { viewModel.setProvider(p.id, it) }
        }
        if ("google" in settings.providersAllowed) {
            HealthSwitchRow("My Google key is on a paid plan", settings.googlePaidKeyConfirmed) { viewModel.setGooglePaid(it) }
        }
        if (state.providers.none { !it.cloud } && settings.providersAllowed.isEmpty()) {
            HealthNote("Health answers are off until you allow a provider or add a local model.", HealthAmber)
        }
        Spacer(Modifier.height(6.dp))
        HealthNote("Local AI only:")
        for (cat in WellnessCategory.entries) {
            HealthSwitchRow(cat.label, settings.localOnly[cat] ?: true) { viewModel.setLocalOnly(cat, it) }
        }
    }

    InstinctPromptCard(settings.instinctPrompt, viewModel)

    HealthCard("Where you can ask") {
        HealthNote("This phone, always.")
        HealthNote("Approved browsers: approving the Portal on a desktop browser comes in a later update.", Neutral700)
    }

    HealthCard("Morning briefing") {
        HealthNote("Off. A short private morning briefing arrives in a later update.", Neutral700)
    }

    HealthCard("Who saw my health data") {
        if (state.disclosures.isEmpty()) HealthNote("No AI has been given your health data yet.")
        for (d in state.disclosures.take(50)) {
            HealthNote("${d.time} · ${d.provider}${if (d.model.isNotBlank()) " ${d.model}" else ""} · ${d.surface} · " +
                d.categories.joinToString(", ").ifBlank { d.tool })
        }
    }

    StopControls(state, viewModel)

    HealthButton("Privacy policy", color = Neutral700) { HealthIntents.openPrivacyPolicy(context) }
}

@Composable
private fun InstinctPromptCard(current: String?, viewModel: WellnessViewModel) {
    var text by remember(current) { mutableStateOf(current ?: "") }
    HealthCard("Health instinct") {
        HealthNote("What the assistant keeps in mind about your health data. Leave empty to use the default.")
        OutlinedTextField(
            value = text,
            onValueChange = { text = WellnessRules.sanitizeInstinctPrompt(it) },
            modifier = Modifier.fillMaxWidth(),
            minLines = 4,
            placeholder = { Text(WellnessRules.DEFAULT_INSTINCT_PROMPT, color = Neutral500) },
            supportingText = { Text("${text.length} / ${WellnessRules.INSTINCT_PROMPT_MAX}", color = Neutral500) },
            colors = OutlinedTextFieldDefaults.colors(
                focusedTextColor = BbxWhite, unfocusedTextColor = BbxWhite,
                focusedBorderColor = BbxAccent, unfocusedBorderColor = Neutral300, cursorColor = BbxAccent,
            ),
        )
        Row {
            HealthButton("Save", enabled = text != (current ?: "")) { viewModel.saveInstinctPrompt(text) }
            HealthButton("Reset to default", color = Neutral700) { text = ""; viewModel.resetInstinctPrompt() }
        }
    }
}

@Composable
private fun StopControls(state: WellnessUiState, viewModel: WellnessViewModel) {
    var disconnectOpen by remember { mutableStateOf(false) }
    var eraseOpen by remember { mutableStateOf(false) }
    HealthCard("Pause or stop") {
        if (state.chip == WellnessRules.StatusChip.PAUSED) HealthButton("Resume") { viewModel.resume() }
        else HealthButton("Pause") { viewModel.pause() }
        HealthButton("Disconnect…") { disconnectOpen = true }
        HealthButton("Stop & erase…") { eraseOpen = true }
    }
    if (disconnectOpen) {
        // No default: the user must pick Keep or Delete history.
        AlertDialog(
            onDismissRequest = { disconnectOpen = false },
            title = { Text("Disconnect this phone?") },
            text = { Text("Collection stops. Do you want to keep what BlackBox already stored, or delete it?") },
            confirmButton = {
                TextButton(onClick = { disconnectOpen = false; viewModel.disconnect(keepHistory = false) }) {
                    Text("Delete history", color = BbxAccent)
                }
            },
            dismissButton = {
                Row {
                    TextButton(onClick = { disconnectOpen = false }) { Text("Cancel", color = Neutral700) }
                    TextButton(onClick = { disconnectOpen = false; viewModel.disconnect(keepHistory = true) }) {
                        Text("Keep history", color = BbxWhite)
                    }
                }
            },
            containerColor = BbxBlack,
        )
    }
    if (eraseOpen) {
        var typed by remember { mutableStateOf("") }
        AlertDialog(
            onDismissRequest = { eraseOpen = false },
            title = { Text("Stop & erase") },
            text = {
                Column {
                    Text("This deletes your health store on the box and this phone's key. Your past health " +
                        "conversations stay in your BlackBox memory, but they contain no health numbers.")
                    Spacer(Modifier.height(8.dp))
                    OutlinedTextField(value = typed, onValueChange = { typed = it }, singleLine = true,
                        label = { Text("Type ${WellnessRules.ERASE_WORD} to confirm") })
                }
            },
            confirmButton = {
                TextButton(enabled = WellnessRules.eraseConfirmed(typed), onClick = { eraseOpen = false; viewModel.erase(typed) }) {
                    Text("Erase", color = if (WellnessRules.eraseConfirmed(typed)) BbxAccent else Neutral500)
                }
            },
            dismissButton = { TextButton(onClick = { eraseOpen = false }) { Text("Cancel", color = Neutral700) } },
            containerColor = BbxBlack,
        )
    }
}
