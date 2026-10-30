package com.aiblackbox.portal.ui.wellness

import android.app.Application
import android.content.Context
import android.os.PowerManager
import android.provider.Settings
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aiblackbox.portal.data.store.BlackBoxStore
import com.aiblackbox.portal.data.wellness.CapabilityProbe
import com.aiblackbox.portal.data.wellness.HcFeature
import com.aiblackbox.portal.data.wellness.HcFeatureStatus
import com.aiblackbox.portal.data.wellness.HcSdkStatus
import com.aiblackbox.portal.data.wellness.IngestClient
import com.aiblackbox.portal.data.wellness.OriginReport
import com.aiblackbox.portal.data.wellness.WellnessBoxStatus
import com.aiblackbox.portal.data.wellness.WellnessCatalog
import com.aiblackbox.portal.data.wellness.WellnessCategory
import com.aiblackbox.portal.data.wellness.WellnessConsent
import com.aiblackbox.portal.data.wellness.WellnessDisclosure
import com.aiblackbox.portal.data.wellness.WellnessEnrollment
import com.aiblackbox.portal.data.wellness.WellnessGraph
import com.aiblackbox.portal.data.wellness.WellnessProbe
import com.aiblackbox.portal.data.wellness.WellnessRules
import com.aiblackbox.portal.data.wellness.WellnessScheduler
import com.aiblackbox.portal.data.wellness.WellnessStatusParser
import com.aiblackbox.portal.data.wellness.WellnessSyncState
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.io.File

/** A provider row on "Which AI can see it" (spec §4.6). */
data class WellnessProviderOption(val id: String, val label: String, val note: String, val cloud: Boolean)

data class WellnessSyncInfo(
    val lastPushMs: Long = 0L,
    val pending: Int = 0,
    val quarantined: Int = 0,
    val intervalMin: Int = WellnessRules.INTERVAL_DEFAULT,
    val backfillDone: Boolean = false,
    val backfillPagesDone: Int = 0,
    val backfillPagesTotal: Int = 0,
    val earliestAvailableMs: Long? = null,
    val hrOriginChoice: String = "auto",
    val hrOriginAuto: String? = null,
)

/** The setup flow's working state (spec §6.1, nine steps). */
data class WellnessSetupUi(
    val step: WellnessRules.SetupStep = WellnessRules.SetupStep.WHAT_THIS_IS,
    val attested: Boolean = false,
    val consent: Map<WellnessCategory, WellnessConsent> = WellnessCatalog.DEFAULT_CONSENT,
    val localOnly: Map<WellnessCategory, Boolean> = WellnessCatalog.DEFAULT_LOCAL_ONLY,
    val wantHistory: Boolean = false,
    val wantBackground: Boolean = false,
    val permissionsAsked: Boolean = false,
    val providersAllowed: Set<String> = emptySet(),
    val googlePaidKeyConfirmed: Boolean = false,
    val assignToMe: Boolean = true,
    val replace: Boolean = false,
    val enrolling: Boolean = false,
    val outcome: String? = null,
    val enrolled: Boolean = false,
)

data class WellnessUiState(
    val loading: Boolean = true,
    val operator: String = "",
    val paired: Boolean = false,
    val sdk: HcSdkStatus = HcSdkStatus.UNAVAILABLE,
    val historyFeature: HcFeatureStatus = HcFeatureStatus.UNAVAILABLE,
    val backgroundFeature: HcFeatureStatus = HcFeatureStatus.UNAVAILABLE,
    val granted: Set<String> = emptySet(),
    val boundOperator: String? = null,
    val epoch: Int? = null,
    val box: WellnessBoxStatus? = null,
    val settings: WellnessRules.Settings = WellnessRules.Settings(),
    val chip: WellnessRules.StatusChip = WellnessRules.StatusChip.NOT_SET_UP,
    val troubles: List<WellnessRules.TroubleCard> = emptyList(),
    val sync: WellnessSyncInfo = WellnessSyncInfo(),
    val sources: List<OriginReport> = emptyList(),
    val hrModes: Map<String, String> = emptyMap(),
    val samsungInstalled: Boolean = false,
    val samsungVersion: String? = null,
    val providers: List<WellnessProviderOption> = emptyList(),
    val disclosures: List<WellnessDisclosure> = emptyList(),
    val setup: WellnessSetupUi = WellnessSetupUi(),
    val setupOpen: Boolean = false,
    val busy: Boolean = false,
    val message: String? = null,
    val probeReport: String? = null,
    val probeFile: String? = null,
    /** The state the screen shows (WellnessRules.shownState): the box's, or the phone's own. */
    val shownState: WellnessRules.EnrollmentState = WellnessRules.EnrollmentState.NOT_SET_UP,
    /** The box names a privacy policy the person has not agreed to: the collector holds (WellnessRules.policyHold). */
    val policyHold: Boolean = false,
    /** The box's current policy text (GET /wellness/privacy-policy) and the version it names, while [policyHold]. */
    val policyText: String? = null,
    val policyTextVersion: String? = null,
) {
    val bound: Boolean get() = boundOperator != null
}

/**
 * Backs Settings, Health and the setup flow. It gathers facts (Health Connect, the binding,
 * the box's status) and performs actions (enroll, settings, pause, erase); every DECISION is
 * a [WellnessRules] call. No health value is ever held here: the screen shows statuses,
 * sources, counts and the disclosure log.
 */
class WellnessViewModel(application: Application) : AndroidViewModel(application) {

    private val app = application
    private val _state = MutableStateFlow(WellnessUiState())
    val state: StateFlow<WellnessUiState> = _state.asStateFlow()

    private val gateway by lazy { WellnessGraph.gateway(app) }
    private val binding by lazy { WellnessGraph.binding(app) }
    private val store by lazy { WellnessGraph.state(app) }
    private val json = Json { ignoreUnknownKeys = true }

    private fun client(): IngestClient? = WellnessGraph.client(app)

    private var lastProbe: CapabilityProbe.Result? = null
    private var lastProbeMs = 0L

    /**
     * Every write to the collector's state file goes through here: off the main thread (it
     * fsyncs) and never able to crash the screen (the listener and the workers write the same
     * file). False when the write failed.
     */
    private suspend fun writeState(change: (WellnessSyncState) -> WellnessSyncState): Boolean =
        withContext(Dispatchers.IO) { runCatching { store.update(change) }.isSuccess }

    fun refresh() = viewModelScope.launch {
        val operator = runCatching { BlackBoxStore(app).operator.first() }.getOrDefault("")
        val sdk = gateway.sdkStatus()
        val available = sdk == HcSdkStatus.AVAILABLE
        val granted = if (available) runCatching { gateway.grantedPermissions() }.getOrDefault(emptySet()) else emptySet()
        val history = if (available) runCatching { gateway.featureStatus(HcFeature.HISTORY_READ) }
            .getOrDefault(HcFeatureStatus.UNAVAILABLE) else HcFeatureStatus.UNAVAILABLE
        val background = if (available) runCatching { gateway.featureStatus(HcFeature.BACKGROUND_READ) }
            .getOrDefault(HcFeatureStatus.UNAVAILABLE) else HcFeatureStatus.UNAVAILABLE
        val record = binding.current()
        val client = client()
        val signer = binding.signer()
        val statusOperator = record?.operator ?: operator
        var statusPolicy: String? = null
        var box = if (client != null && statusOperator.isNotBlank()) {
            val reply = client.status(statusOperator, signer)
            if (reply.ok) WellnessStatusParser.parse(reply.body).also { parsed ->
                statusPolicy = reply.policyVersion ?: parsed?.takeIf { it.credentialed }?.policyVersion
            } else null
        } else null
        // A credentialed status names the box's policy version: a new one holds the collector.
        if (record != null && statusPolicy != null) writeState { WellnessRules.noteBoxPolicy(it, statusPolicy) }
        // A replacement the old phone has approved: the AI choices setup made while it waited
        // go now (the collector's status read sends them too, whichever comes first).
        if (record != null && client != null && signer != null) {
            val pending = withContext(Dispatchers.IO) { store.load() }.pendingSettings
            WellnessRules.pendingSettingsBody(record.operator, pending, box)?.let { body ->
                val put = client.putSettings(body, signer)
                when (WellnessRules.pendingSettingsOutcome(put.status)) {
                    WellnessRules.PendingSettingsOutcome.SAVED -> {
                        writeState { it.copy(pendingSettings = null) }
                        box = WellnessStatusParser.parse(put.body)?.takeIf { it.credentialed } ?: box
                    }
                    WellnessRules.PendingSettingsOutcome.REFUSED -> {
                        writeState { it.copy(pendingSettings = null) }
                        say(WellnessRules.PENDING_SETTINGS_REFUSED_MESSAGE)
                    }
                    WellnessRules.PendingSettingsOutcome.RETRY -> Unit
                }
            }
        }
        // The box's consent is the truth; the collector reads what it says.
        if (box?.categories != null) {
            writeState { st ->
                st.copy(
                    categories = box.categories.mapKeys { it.key.wire }.mapValues { it.value.wire },
                    state = box.state.wire,
                    sourceLabel = box.sourceLabel ?: st.sourceLabel,
                )
            }
        }
        val disclosures = if (client != null && signer != null && record != null) {
            val reply = client.get(IngestClient.DISCLOSURES + "?operator=" + java.net.URLEncoder.encode(record.operator, "UTF-8"), signer)
            if (reply.ok) WellnessStatusParser.parseDisclosures(reply.body) else emptyList()
        } else emptyList()
        // The source card comes from the capability probe (counts only). It reads 14 days of
        // records, so it runs at most every few minutes, and only once something is granted.
        val now = System.currentTimeMillis()
        val probe = if (granted.isEmpty()) null else lastProbe?.takeIf { now - lastProbeMs < PROBE_EVERY_MS }
            ?: runCatching { withContext(Dispatchers.IO) { WellnessGraph.probe(app).run() } }.getOrNull()?.also {
                lastProbe = it
                lastProbeMs = now
                runCatching { withContext(Dispatchers.IO) { WellnessGraph.applyProbe(app, it) } }
            }
        val st = withContext(Dispatchers.IO) { store.load() }
        val outbox = WellnessGraph.outbox(app)
        val consented = st.consent().filterValues { it.isOn }.keys
        val local = WellnessRules.localTroubles(
            WellnessRules.LocalFacts(
                sdk = sdk,
                consentedCategories = if (record != null) consented else emptySet(),
                granted = granted,
                backgroundGranted = WellnessCatalog.PERMISSION_BACKGROUND in granted,
                needsReconnect = st.needsReconnect,
                outboxFull = outbox.isFull(),
                samsungInstalled = probe?.report?.samsungHealth?.installed ?: false,
                samsungWritesToHc = probe?.samsungWritesToHc ?: true,
                batteryExempt = batteryExempt() ?: true,
                periodicOverdue = WellnessRules.periodicOverdue(st.lastPeriodicRunMs, st.intervalMin, System.currentTimeMillis()),
                replacePending = WellnessRules.shownState(box?.state, box?.credentialed == true, st.enrollmentState()) ==
                    WellnessRules.EnrollmentState.REPLACE_PENDING,
            ),
        )
        val troubleCodes = WellnessRules.mergeTroubles(box?.troubles.orEmpty(), if (record != null) local else
            local.filter { it == "hc_unavailable" || it == "hc_needs_update" })
        val boxState = WellnessRules.shownState(box?.state, box?.credentialed == true, st.enrollmentState())
        // The policy changed: the card shows the box's own current text (and agreeing records
        // the version of THAT text, never one the person did not see).
        val policyHold = record != null && WellnessRules.policyHold(st)
        val policy = if (policyHold && client != null) {
            client.get(IngestClient.PRIVACY_POLICY, null).takeIf { it.ok }
        } else null
        _state.update { s ->
            s.copy(
                loading = false,
                operator = operator,
                paired = client != null,
                sdk = sdk,
                historyFeature = history,
                backgroundFeature = background,
                granted = granted,
                boundOperator = record?.operator,
                epoch = record?.epoch,
                box = box,
                settings = box?.takeIf { it.credentialed }?.settings() ?: s.settings,
                chip = WellnessRules.statusChip(boxState, record != null, troubleCodes, held = st.localHold != null,
                    policyHold = policyHold),
                shownState = boxState,
                policyHold = policyHold,
                policyText = policy?.body?.takeIf { it.isNotBlank() },
                policyTextVersion = WellnessRules.policyVersion(policy?.policyVersion),
                troubles = troubleCodes.mapNotNull { WellnessRules.troubleCard(it) },
                sync = WellnessSyncInfo(
                    lastPushMs = st.lastPushMs, pending = outbox.size(), quarantined = outbox.quarantinedCount(),
                    intervalMin = st.intervalMin, backfillDone = st.backfill.done,
                    backfillPagesDone = st.backfill.pagesDone, backfillPagesTotal = st.backfill.pagesTotal,
                    earliestAvailableMs = st.earliestAvailableMs, hrOriginChoice = st.hrOriginChoice,
                    hrOriginAuto = st.hrOriginAuto,
                ),
                sources = probe?.report?.origins.orEmpty(),
                hrModes = probe?.hrModeByPackage.orEmpty(),
                samsungInstalled = probe?.report?.samsungHealth?.installed ?: false,
                samsungVersion = probe?.report?.samsungHealth?.version,
                disclosures = disclosures,
            )
        }
        loadProviders()
    }

    /** The cloud providers of the contract plus this box's custom servers (GET /models/custom). */
    private fun loadProviders() = viewModelScope.launch {
        val aliases = runCatching {
            val reply = client()?.get("/models/custom", null)
            if (reply == null || !reply.ok) emptyList() else
                (json.parseToJsonElement(reply.body).jsonObject["models"] as? JsonArray)
                    ?.mapNotNull { (it as? JsonObject)?.get("server")?.jsonPrimitive?.contentOrNull }
                    ?.distinct().orEmpty()
        }.getOrDefault(emptyList())
        val options = CLOUD_PROVIDERS + aliases.map {
            WellnessProviderOption("custom:$it", "$it (your server)",
                "On your network if its address is private; the box checks. Tick only if it is a rented or public server.",
                cloud = false)
        }
        _state.update { it.copy(providers = options) }
    }

    fun clearMessage() = _state.update { it.copy(message = null) }

    private fun say(text: String) = _state.update { it.copy(message = text) }

    // ── setup ──────────────────────────────────────────────────────────────────────────────

    /** Open the setup flow; "Reconnect Health" re-enrolls this phone with replace. */
    fun startSetup() = _state.update {
        it.copy(
            setupOpen = true,
            setup = WellnessSetupUi(replace = it.box?.enrolled == true || it.bound,
                consent = it.settings.categories, localOnly = it.settings.localOnly,
                providersAllowed = it.settings.providersAllowed),
        )
    }

    fun closeSetup() {
        _state.update { it.copy(setupOpen = false) }
        refresh()
    }

    fun setupUpdate(change: (WellnessSetupUi) -> WellnessSetupUi) = _state.update { it.copy(setup = change(it.setup)) }

    fun setupState(): WellnessRules.SetupState = state.value.let { s ->
        WellnessRules.SetupState(
            attested = s.setup.attested, sdk = s.sdk, paired = s.paired, operator = s.operator,
            consent = s.setup.consent, granted = s.granted, enrolled = s.setup.enrolled,
        )
    }

    fun nextStep() {
        val s = state.value
        WellnessRules.blockerFor(s.setup.step, setupState())?.let { say(it); return }
        val next = WellnessRules.nextStep(s.setup.step) ?: return
        setupUpdate { it.copy(step = next) }
        if (next == WellnessRules.SetupStep.DONE) enroll()
    }

    fun previousStep() {
        val prev = WellnessRules.previousStep(state.value.setup.step) ?: return
        setupUpdate { it.copy(step = prev) }
    }

    /** The exact set for the Health Connect dialog (WellnessRules decides). */
    fun permissionsToRequest(): Set<String> = state.value.let { s ->
        WellnessRules.permissionsToRequest(s.setup.consent, s.setup.wantHistory, s.setup.wantBackground,
            s.historyFeature, s.backgroundFeature)
    }

    fun onPermissionsResult(granted: Set<String>) {
        setupUpdate { it.copy(permissionsAsked = true) }
        _state.update { it.copy(granted = it.granted + granted) }
        viewModelScope.launch {
            val fresh = runCatching { gateway.grantedPermissions() }.getOrDefault(state.value.granted)
            _state.update { it.copy(granted = fresh) }
            // A permission change re-sends the capability report and syncs (spec §3.2).
            if (binding.current() != null) {
                runCatching { WellnessGraph.pushCapabilities(app, force = true) }
                WellnessScheduler.syncNow(app)
            }
        }
    }

    private fun enroll() = viewModelScope.launch {
        val s = state.value
        val client = client() ?: run { say("Pair this phone with your box first."); return@launch }
        setupUpdate { it.copy(enrolling = true, outcome = null) }
        writeState { it.copy(categories = s.setup.consent.mapKeys { e -> e.key.wire }.mapValues { e -> e.value.wire }) }
        val report = runCatching { withContext(Dispatchers.IO) { WellnessGraph.probe(app).run() } }.getOrNull()
        report?.let { runCatching { withContext(Dispatchers.IO) { WellnessGraph.applyProbe(app, it) } } }
        // A bind this attempt received but the box did not confirm is rolled back with
        // whatever it scheduled (nothing is scheduled before the box's 200).
        // The bind is pinned to the box this enroll call goes to (contract (f)).
        val enrollment = WellnessEnrollment(binding, { body -> client.enroll(body) }, { WellnessGraph.tailnetIps() },
            onRolledBack = { WellnessScheduler.cancelAll(app) }, boxOrigin = client.origin)
        val outcome = enrollment.enroll(
            operator = s.operator,
            consent = s.setup.consent,
            capabilityReportJson = report?.let { CapabilityProbe.toJson(it.report) } ?: "{}",
            assignToMe = s.setup.assignToMe,
            replace = s.setup.replace,
        )
        if (outcome is WellnessEnrollment.Outcome.Enrolled) {
            // The person agreed to the privacy policy this enrollment was made under: the version
            // the enroll reply carries (a later one on the box holds the collector until agreed).
            writeState { WellnessRules.enrolledUnderPolicy(it, outcome.policyVersion) }
        }
        // Providers and local-only are settings, sent signed once the box takes them from this phone.
        val wanted = WellnessRules.Settings(
            categories = s.setup.consent, localOnly = s.setup.localOnly,
            providersAllowed = s.setup.providersAllowed, googlePaidKeyConfirmed = s.setup.googlePaidKeyConfirmed,
        )
        if (outcome is WellnessEnrollment.Outcome.Enrolled && outcome.awaitsApproval) {
            // A replacement of a source that is still online: the box takes nothing from this
            // phone until the old one approves, and the approval keeps the OLD enrollment's AI
            // choices. So the ones made here wait in the collector's state and go in one signed
            // PUT once this phone's own signed status is credentialed (the periodic sync's
            // status read, or this screen's refresh; WellnessRules.pendingSettingsBody).
            outcome.syncIntervalMin?.let { m -> writeState { it.copy(intervalMin = WellnessRules.clampInterval(m)) } }
            writeState {
                it.copy(state = WellnessRules.EnrollmentState.REPLACE_PENDING.wire,
                    pendingSettings = WellnessRules.pendingSettingsOf(wanted))
            }
            WellnessScheduler.onEnrolled(app)
        } else if (outcome is WellnessEnrollment.Outcome.Enrolled) {
            outcome.syncIntervalMin?.let { m -> writeState { it.copy(intervalMin = WellnessRules.clampInterval(m)) } }
            // An earlier attempt's waiting choices must never be sent over these later.
            writeState { it.copy(pendingSettings = null) }
            // Now that this phone holds the key: one signed PUT.
            val body = WellnessStatusParser.settingsBody(s.operator, wanted, wanted, WellnessStatusParser.SETUP_FIELDS)
            val put = binding.signer()?.let { client.putSettings(body, it) }
            if (put?.ok != true) say("Health is set up, but your AI choices didn't save. Set them again in Health settings.")
            // The box has stored the key: now the first sync, the periodic job and the import.
            WellnessScheduler.onEnrolled(app)
        }
        setupUpdate {
            it.copy(enrolling = false, outcome = outcome.message, enrolled = outcome is WellnessEnrollment.Outcome.Enrolled,
                step = if (outcome is WellnessEnrollment.Outcome.Enrolled) WellnessRules.SetupStep.DONE else it.step)
        }
        if (outcome !is WellnessEnrollment.Outcome.Enrolled) {
            setupUpdate { it.copy(step = WellnessRules.SetupStep.MORNING_BRIEFING) }
            say(outcome.message)
        }
        refresh()
    }

    // ── settings (narrowing from anywhere; this phone signs every call) ────────────────────

    private fun applySettings(change: (WellnessRules.Settings) -> WellnessRules.Settings) = viewModelScope.launch {
        val s = state.value
        val record = binding.current() ?: run { say("Set up Health on this phone first."); return@launch }
        val client = client() ?: return@launch
        // Edit only from the box's own view of the settings: a diff against the phone's defaults
        // could carry a change (even a widening one) the person never made.
        if (s.box?.credentialed != true) {
            say("Couldn't read your Health settings from the box. Try again in a moment.")
            refresh()
            return@launch
        }
        val old = s.settings
        val new = change(old)
        if (new == old) return@launch
        _state.update { it.copy(busy = true) }
        val reply = client.putSettings(WellnessStatusParser.settingsBody(record.operator, old, new), binding.signer())
        val parsed = if (reply.ok) WellnessStatusParser.parse(reply.body) else null
        _state.update {
            it.copy(busy = false, settings = parsed?.takeIf { p -> p.credentialed }?.settings() ?: if (reply.ok) new else old,
                box = parsed ?: it.box)
        }
        if (!reply.ok) say(if (reply.reached) "The box didn't accept that change." else "Couldn't reach your box.")
        else {
            // The collector filters every push by this, so a category turned off here is never
            // delivered from the outbox afterwards.
            writeState { st -> st.copy(categories = new.categories.mapKeys { e -> e.key.wire }.mapValues { e -> e.value.wire }) }
            // A category turned on needs its Health Connect permission: the screen offers it.
        }
    }

    fun setConsent(category: WellnessCategory, consent: WellnessConsent) =
        applySettings { it.copy(categories = it.categories + (category to consent)) }

    fun setLocalOnly(category: WellnessCategory, on: Boolean) =
        applySettings { it.copy(localOnly = it.localOnly + (category to on)) }

    fun setProvider(id: String, allowed: Boolean) =
        applySettings { it.copy(providersAllowed = if (allowed) it.providersAllowed + id else it.providersAllowed - id) }

    /**
     * "Only AI on my network": no tick at all. Local and on-device destinations are allowed
     * without one, and a custom server on a public address counts as cloud, so clearing every
     * tick is exactly "nothing leaves my network". Turning it off widens nothing by itself.
     */
    fun setOnlyLocal(on: Boolean) {
        if (!on) return
        applySettings { s -> s.copy(providersAllowed = emptySet()) }
    }

    fun setGooglePaid(on: Boolean) = applySettings { it.copy(googlePaidKeyConfirmed = on) }

    fun saveInstinctPrompt(text: String) =
        applySettings { it.copy(instinctPrompt = WellnessRules.sanitizeInstinctPrompt(text).takeIf { t -> t.isNotBlank() }) }

    fun resetInstinctPrompt() = applySettings { it.copy(instinctPrompt = null) }

    // ── sync ───────────────────────────────────────────────────────────────────────────────

    fun syncNow() = viewModelScope.launch {
        if (withContext(Dispatchers.IO) { binding.current() } == null) return@launch
        writeState { it.copy(needsReconnect = false) }
        WellnessScheduler.syncNow(app)
        say("Syncing health data now.")
    }

    fun setInterval(minutes: Int) = viewModelScope.launch {
        val m = WellnessRules.clampInterval(minutes)
        _state.update { it.copy(sync = it.sync.copy(intervalMin = m)) }
        writeState { it.copy(intervalMin = m) }
        if (withContext(Dispatchers.IO) { binding.current() } != null) WellnessScheduler.schedulePeriodic(app, m)
    }

    fun setHeartRateOrigin(choice: String) = viewModelScope.launch {
        _state.update { it.copy(sync = it.sync.copy(hrOriginChoice = choice)) }
        writeState { it.copy(hrOriginChoice = choice) }
    }

    // ── pause / disconnect / erase ─────────────────────────────────────────────────────────

    private fun body(operator: String, extra: (JsonObjectBuilderScope) -> Unit): String = buildJsonObject {
        put("operator", JsonPrimitive(operator))
        extra(JsonObjectBuilderScope { k, v -> put(k, v) })
    }.toString()

    /**
     * Pause, Disconnect and Stop & erase take effect ON THIS PHONE first, whatever the box
     * answers: the hold ([WellnessSyncState.localHold]) stops every read and push before the
     * request goes out. Then the box is told, signed with the key, which stays until the box
     * confirms (erase needs it, and a retry must be possible). A box that no longer accepts
     * the key at all ends Health on this phone (WellnessRules.narrowingOutcome).
     */
    private fun narrow(path: String, hold: String, extra: (JsonObjectBuilderScope) -> Unit = {},
                       onConfirmed: suspend () -> Unit) = viewModelScope.launch {
        val record = withContext(Dispatchers.IO) { binding.current() } ?: run { say("Health isn't set up on this phone."); return@launch }
        val signer = binding.signer()
        _state.update { it.copy(busy = true) }
        writeState { it.copy(localHold = hold) }
        if (hold != WellnessRules.HOLD_PAUSE) WellnessScheduler.cancelAll(app)
        val client = client()
        val requestBody = body(record.operator, extra)
        var reply = client?.action(path, requestBody, signer) ?: com.aiblackbox.portal.data.wellness.WellnessHttp(-1, "")
        // A 401 can be this phone's clock: the refusal's Date header has just corrected it, so
        // one signed retry tells a drifted clock from a key the box no longer accepts.
        if (reply.status == 401 && client != null) reply = client.action(path, requestBody, signer)
        _state.update { it.copy(busy = false) }
        when (WellnessRules.narrowingOutcome(reply.status)) {
            WellnessRules.NarrowingOutcome.CONFIRMED -> onConfirmed()
            WellnessRules.NarrowingOutcome.KEY_REFUSED -> {
                WellnessScheduler.cancelAll(app)
                withContext(Dispatchers.IO) { runCatching { binding.clearAll() } }
                say(if (hold == WellnessRules.HOLD_PAUSE) "Your box no longer accepts this phone for Health, so Health was removed from it."
                    else "Your box no longer accepts this phone's key, so Health was removed from this phone. Anything the " +
                        "box already stored stays there until you set Health up again and erase it.")
            }
            WellnessRules.NarrowingOutcome.UNCONFIRMED -> say(
                if (reply.reached) "Stopped on this phone. The box didn't accept the change yet; try again."
                else "Stopped on this phone. Couldn't reach your box; try again when it's reachable.")
        }
        refresh()
    }

    fun pause() = narrow(IngestClient.PAUSE, WellnessRules.HOLD_PAUSE) {
        writeState { it.copy(localHold = null, state = WellnessRules.EnrollmentState.PAUSED.wire) }
        say("Health collection paused.")
    }

    /** Resume widens, so it needs the box's yes first (signed); it also lifts a hold. */
    fun resume() = viewModelScope.launch {
        val record = withContext(Dispatchers.IO) { binding.current() } ?: return@launch
        val client = client() ?: return@launch
        _state.update { it.copy(busy = true) }
        val reply = client.action(IngestClient.RESUME, body(record.operator) {}, binding.signer())
        _state.update { it.copy(busy = false) }
        if (reply.ok) {
            writeState { it.copy(localHold = null, state = WellnessRules.EnrollmentState.ACTIVE.wire) }
            WellnessScheduler.syncNow(app)
            say("Health collection resumed.")
        } else {
            say(if (reply.reached) "The box didn't accept that." else "Couldn't reach your box.")
        }
        refresh()
    }

    /**
     * "I agree" on "The privacy policy changed": records the version of the text the card showed
     * (WellnessRules.agreePolicy) and lets the collector run again. Only over text the box sent
     * (WellnessRules.mayAgreeToPolicy); "Disconnect" is the other answer.
     */
    fun agreeToPolicy() = viewModelScope.launch {
        val s = state.value
        val version = s.policyTextVersion
        if (!WellnessRules.mayAgreeToPolicy(s.policyText, version) || version == null) {
            say(WellnessRules.POLICY_TEXT_UNAVAILABLE)
            return@launch
        }
        if (!writeState { WellnessRules.agreePolicy(it, version) }) {
            say("Couldn't save that on this phone. Try again.")
            return@launch
        }
        WellnessScheduler.syncNow(app)
        say("Thanks. Health collection is back on.")
        refresh()
    }

    /** [keepHistory] must be an explicit choice (WellnessRules.disconnectAllowed). */
    fun disconnect(keepHistory: Boolean?) {
        if (!WellnessRules.disconnectAllowed(keepHistory)) { say("Choose Keep history or Delete history."); return }
        narrow(IngestClient.DISCONNECT, WellnessRules.HOLD_DISCONNECT,
            extra = { it.put("keep_history", JsonPrimitive(keepHistory!!)) }) {
            WellnessScheduler.cancelAll(app)
            withContext(Dispatchers.IO) { runCatching { binding.clearAll() } }
            say(if (keepHistory == true) "Disconnected. Your stored history stays on the box."
                else "Disconnected and your stored health history was deleted.")
        }
    }

    /**
     * The OLD phone's answer to a replacement (spec §3.1): another phone of this operator asked
     * to become the health source while this one is online, and the box waits for this tap.
     * Approve moves Health (history, settings) to the new phone and the box revokes this
     * phone's key, so Health is forgotten here; decline keeps this phone the source.
     */
    fun answerReplacement(approve: Boolean) = viewModelScope.launch {
        val record = withContext(Dispatchers.IO) { binding.current() } ?: run { say("Health isn't set up on this phone."); return@launch }
        val client = client() ?: run { say("Couldn't reach your box."); return@launch }
        _state.update { it.copy(busy = true) }
        val body = WellnessStatusParser.replaceApproveBody(record.operator, approve)
        var reply = client.action(IngestClient.REPLACE_APPROVE, body, binding.signer())
        // As for a narrowing: one retry after a 401 tells a drifted clock (just corrected
        // from the refusal's Date header) from a key the box no longer accepts.
        if (reply.status == 401) reply = client.action(IngestClient.REPLACE_APPROVE, body, binding.signer())
        _state.update { it.copy(busy = false) }
        val outcome = WellnessRules.replaceOutcome(reply.status, com.aiblackbox.portal.data.wellness.WellnessOutbox.parseError(reply.body), approve)
        if (WellnessRules.replaceClearsHere(outcome)) {
            WellnessScheduler.cancelAll(app)
            withContext(Dispatchers.IO) { runCatching { binding.clearAll() } }
        }
        say(outcome.message)
        refresh()
    }

    fun erase(typed: String) {
        if (!WellnessRules.eraseConfirmed(typed)) { say("Type ${WellnessRules.ERASE_WORD} to confirm."); return }
        narrow(IngestClient.ERASE, WellnessRules.HOLD_ERASE) {
            WellnessScheduler.cancelAll(app)
            withContext(Dispatchers.IO) { runCatching { binding.clearAll() } }
            say("Erased. Your past health conversations stay in your BlackBox memory, but they contain no health numbers.")
        }
    }

    // ── the M7.1 probe (debug builds, or a build made with -PwellnessProbe) ─────────────────

    fun runProbe() = viewModelScope.launch {
        _state.update { it.copy(busy = true, probeReport = null, probeFile = null) }
        val report = withContext(Dispatchers.IO) {
            runCatching {
                WellnessProbe(
                    gateway = gateway,
                    packages = com.aiblackbox.portal.data.wellness.AndroidPackageInfoSource(app),
                    phone = { WellnessGraph.phoneInfo(app) },
                    state = { store.load() },
                    env = object : WellnessProbe.Environment {
                        override fun deviceNameReadable(): Boolean = runCatching {
                            !Settings.Global.getString(app.contentResolver, "device_name").isNullOrBlank()
                        }.getOrDefault(false)
                        override fun batteryExempt(): Boolean? = this@WellnessViewModel.batteryExempt()
                        override fun tailnetIp(): String? = WellnessGraph.tailnetIps().firstOrNull()
                    },
                ).run()
            }.getOrNull()
        }
        if (report == null) {
            _state.update { it.copy(busy = false) }
            say("The probe could not run.")
            return@launch
        }
        val pretty = Json { prettyPrint = true }.encodeToString(JsonObject.serializer(), report)
        // The export holds counts, dates and gaps (no values), but it is still about the
        // person's health: no-backup storage like every wellness file, erased with them. The
        // share sheet sends the text itself.
        val file = withContext(Dispatchers.IO) {
            runCatching {
                val dir = WellnessGraph.files(app).probeExports.apply { mkdirs() }
                File(dir, "probe-${System.currentTimeMillis()}.json").apply { writeText(pretty) }
            }.getOrNull()
        }
        _state.update { it.copy(busy = false, probeReport = pretty, probeFile = file?.absolutePath) }
    }

    private fun batteryExempt(): Boolean? = runCatching {
        (app.getSystemService(Context.POWER_SERVICE) as PowerManager).isIgnoringBatteryOptimizations(app.packageName)
    }.getOrNull()

    /** A tiny seam so [action] callers can add fields without importing the builder type. */
    fun interface JsonObjectBuilderScope {
        fun put(key: String, value: JsonPrimitive)
    }

    companion object {
        private const val PROBE_EVERY_MS = 5 * 60_000L

        /** The contract's cloud provider ids, each with its one-line terms note (spec §4.6). */
        val CLOUD_PROVIDERS = listOf(
            WellnessProviderOption("anthropic", "Anthropic", "Not used for training without your permission.", cloud = true),
            WellnessProviderOption("openai", "OpenAI", "Not used for training by default; abuse logs kept up to 30 days.", cloud = true),
            WellnessProviderOption("google", "Google", "Only with a paid key (tick \"My Google key is on a paid plan\").", cloud = true),
            WellnessProviderOption("xai", "xAI", "See xAI's terms before allowing.", cloud = true),
        )
    }
}
