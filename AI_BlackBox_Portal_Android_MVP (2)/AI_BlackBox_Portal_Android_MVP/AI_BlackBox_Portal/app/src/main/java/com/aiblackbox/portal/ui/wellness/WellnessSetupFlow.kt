package com.aiblackbox.portal.ui.wellness

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CheckboxDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.health.connect.client.PermissionController
import com.aiblackbox.portal.data.wellness.HcSdkStatus
import com.aiblackbox.portal.data.wellness.WellnessCatalog
import com.aiblackbox.portal.data.wellness.WellnessCategory
import com.aiblackbox.portal.data.wellness.WellnessConsent
import com.aiblackbox.portal.data.wellness.WellnessRules
import com.aiblackbox.portal.data.wellness.WellnessRules.SetupStep
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.Neutral700

/**
 * Setup, one screen per step with Back on each (spec §6.1):
 *  1 what this is + the attestation, 2 this phone, 3 what to share, 4 your privacy,
 *  5 past data and background (cards only when Health Connect says AVAILABLE) then the Health
 *  Connect dialog for the CHOSEN types only, 6 which AI, 7 where you can ask, 8 briefing
 *  (M7.3), 9 done with first-sync progress.
 * Every "may I continue" is [WellnessRules.blockerFor]; this file only draws.
 */
@Composable
fun WellnessSetupFlow(state: WellnessUiState, viewModel: WellnessViewModel, onClose: () -> Unit) {
    val context = LocalContext.current
    val setup = state.setup
    val launcher = rememberLauncherForActivityResult(
        remember { PermissionController.createRequestPermissionResultContract() },
    ) { granted -> viewModel.onPermissionsResult(granted) }

    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(
            "Step ${setup.step.ordinal + 1} of ${SetupStep.entries.size}: ${setup.step.title}",
            style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.SemiBold),
            color = BbxWhite,
        )
        when (setup.step) {
            SetupStep.WHAT_THIS_IS -> HealthCard("What this is") {
                HealthNote("BlackBox Health helps you understand your own wearable data and remembers it alongside " +
                    "your day. It never diagnoses, never shares without your choice, and is not a medical device.")
                Spacer(Modifier.height(8.dp))
                CheckRow("This is my own data, only I use this phone's Health Connect, and I am an adult.", setup.attested) {
                    viewModel.setupUpdate { s -> s.copy(attested = it) }
                }
            }
            SetupStep.THIS_PHONE -> HealthCard("This phone") {
                Check("Health Connect", state.sdk == HcSdkStatus.AVAILABLE, when (state.sdk) {
                    HcSdkStatus.AVAILABLE -> "available"
                    HcSdkStatus.NEEDS_UPDATE -> "needs an update"
                    HcSdkStatus.UNAVAILABLE -> "not available"
                })
                if (state.sdk != HcSdkStatus.AVAILABLE) HealthButton("Get Health Connect") { HealthIntents.getHealthConnect(context) }
                Check("Samsung Health", state.samsungInstalled,
                    if (state.samsungInstalled) "installed ${state.samsungVersion ?: ""}".trim() else "not installed (other apps work too)")
                Check("Paired with your box", state.paired, if (state.paired) "yes" else "open BlackBox while connected to your box")
                Check("Your operator", state.operator.isNotBlank(), state.operator.ifBlank { "pick one in Settings" })
                CheckRow("Assign this phone to me in Devices if it has no owner yet", setup.assignToMe) {
                    viewModel.setupUpdate { s -> s.copy(assignToMe = it) }
                }
                if (setup.replace || state.box?.enrolled == true) {
                    CheckRow("Replace my current health source with this phone", setup.replace) {
                        viewModel.setupUpdate { s -> s.copy(replace = it) }
                    }
                }
                if (state.samsungInstalled && state.sources.none { it.packageName == WellnessCatalog.SAMSUNG_HEALTH_PACKAGE }) {
                    HealthNote("In Samsung Health, Settings, Health Connect: allow Sleep, Heart rate, Steps, Exercise " +
                        "(and Blood pressure).", HealthAmber)
                    HealthButton("Open Samsung Health") { HealthIntents.openSamsungHealth(context) }
                }
            }
            SetupStep.WHAT_TO_SHARE -> HealthCard("What to share") {
                for (cat in WellnessCategory.entries) {
                    Text(cat.label, style = MaterialTheme.typography.bodyMedium.copy(fontWeight = FontWeight.SemiBold), color = BbxWhite)
                    HealthNote("For example: \"${cat.example}\"", Neutral700)
                    ConsentSelector(setup.consent[cat] ?: WellnessConsent.OFF) { c ->
                        viewModel.setupUpdate { s -> s.copy(consent = s.consent + (cat to c)) }
                    }
                    if (cat == WellnessCategory.BLOOD_PRESSURE) HealthNote("Blood pressure is answered by AI on your own network only, unless you change it later.")
                    Spacer(Modifier.height(8.dp))
                }
                HealthNote("Ask: BlackBox uses it when you ask. Proactive: it may also bring it up when it is relevant.")
            }
            SetupStep.YOUR_PRIVACY -> HealthCard("Your privacy") {
                for (cat in WellnessCategory.entries.filter { (setup.consent[it] ?: WellnessConsent.OFF).isOn }) {
                    Text(cat.label, style = MaterialTheme.typography.bodyMedium.copy(fontWeight = FontWeight.SemiBold), color = BbxWhite)
                    HealthNote("Reads: " + WellnessCatalog.typesFor(listOf(cat)).joinToString(", ") { it.wire.replace('_', ' ') } +
                        ". Goes only to your own box, then only to an AI you allow. Never sold, never used for ads.")
                }
                HealthButton("Read the full privacy policy") { HealthIntents.openPrivacyPolicy(context) }
            }
            SetupStep.PAST_AND_BACKGROUND -> HealthCard("Past data and background access") {
                if (WellnessRules.showPastDataCard(state.historyFeature)) {
                    CheckRow("Past data: let BlackBox read history older than 30 days", setup.wantHistory) {
                        viewModel.setupUpdate { s -> s.copy(wantHistory = it) }
                    }
                }
                if (WellnessRules.showBackgroundCard(state.backgroundFeature)) {
                    CheckRow("Background access: sync while BlackBox is closed (needed for briefings)", setup.wantBackground) {
                        viewModel.setupUpdate { s -> s.copy(wantBackground = it) }
                    }
                    HealthNote("Without it, BlackBox reads only while the app is open.", Neutral700)
                }
                val request = viewModel.permissionsToRequest()
                HealthButton("Choose in Health Connect (${request.size})", enabled = request.isNotEmpty()) {
                    launcher.launch(request)
                }
                val chosen = WellnessCatalog.readPermissionsFor(setup.consent.filterValues { it.isOn }.keys)
                val granted = chosen.count { it in state.granted }
                if (setup.permissionsAsked) HealthNote("$granted of ${chosen.size} kinds allowed in Health Connect.",
                    if (granted == 0) HealthAmber else Neutral700)
            }
            SetupStep.WHICH_AI -> HealthCard("Which AI can see it") {
                HealthSwitchRow("Only AI on my network", setup.providersAllowed.isEmpty(),
                    note = "AI on your own network is always allowed. Cloud AI needs a tick.") { on ->
                    if (on) viewModel.setupUpdate { s -> s.copy(providersAllowed = emptySet()) }
                }
                for (p in state.providers) {
                    HealthSwitchRow(p.label, p.id in setup.providersAllowed, note = p.note) { on ->
                        viewModel.setupUpdate { s ->
                            s.copy(providersAllowed = if (on) s.providersAllowed + p.id else s.providersAllowed - p.id)
                        }
                    }
                }
                if ("google" in setup.providersAllowed) {
                    CheckRow("My Google key is on a paid plan", setup.googlePaidKeyConfirmed) {
                        viewModel.setupUpdate { s -> s.copy(googlePaidKeyConfirmed = it) }
                    }
                }
                HealthNote("Local AI only, per kind of data:")
                for (cat in WellnessCategory.entries.filter { (setup.consent[it] ?: WellnessConsent.OFF).isOn }) {
                    HealthSwitchRow("${cat.label}: local AI only", setup.localOnly[cat] ?: true) { on ->
                        viewModel.setupUpdate { s -> s.copy(localOnly = s.localOnly + (cat to on)) }
                    }
                }
            }
            SetupStep.WHERE_YOU_CAN_ASK -> HealthCard("Where you can ask") {
                HealthNote("This phone, always. Approving a desktop browser comes later, from the Health screen.")
            }
            SetupStep.MORNING_BRIEFING -> HealthCard("Morning briefing") {
                HealthNote("A short private briefing each morning arrives in a later update. It stays off until you turn it on.")
                setup.outcome?.let { HealthNote(it, HealthAmber) }
            }
            SetupStep.DONE -> HealthCard("Done") {
                when {
                    setup.enrolling -> Row(verticalAlignment = Alignment.CenterVertically) {
                        CircularProgressIndicator(color = BbxAccent)
                        HealthNote("  Finishing setup with your box…")
                    }
                    setup.enrolled -> {
                        HealthNote(setup.outcome ?: "This phone is now your health source.")
                        val s = state.sync
                        HealthNote(if (s.backfillPagesTotal > 0) "Importing history: ${s.backfillPagesDone} of ${s.backfillPagesTotal} weeks"
                            else "The first sync is starting.")
                    }
                    else -> HealthNote(setup.outcome ?: "Setup hasn't finished.", HealthAmber)
                }
            }
        }
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            if (setup.step == SetupStep.WHAT_THIS_IS) HealthButton("Cancel", color = Neutral700) { onClose() }
            else if (setup.step != SetupStep.DONE) HealthButton("Back", color = Neutral700) { viewModel.previousStep() }
            else Spacer(Modifier.height(1.dp))
            when (setup.step) {
                SetupStep.DONE -> HealthButton("Close", enabled = !setup.enrolling) { onClose() }
                SetupStep.MORNING_BRIEFING -> HealthButton("Use this phone for my health") { viewModel.nextStep() }
                else -> HealthButton("Next") { viewModel.nextStep() }
            }
        }
    }
}

@Composable
private fun Check(label: String, ok: Boolean, detail: String) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(if (ok) "✓ " else "• ", color = if (ok) BbxAccent else HealthAmber)
        Text("$label: ", style = MaterialTheme.typography.bodyMedium, color = BbxWhite)
        HealthNote(detail)
    }
}

@Composable
private fun CheckRow(label: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Checkbox(checked = checked, onCheckedChange = onChange, colors = CheckboxDefaults.colors(checkedColor = BbxAccent))
        Text(label, style = MaterialTheme.typography.bodyMedium, color = BbxWhite)
    }
}
