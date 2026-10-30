package com.aiblackbox.portal.data.wellness

/**
 * Every decision the Health feature makes on the phone, PURE (no Android types), following
 * the LocationRules pattern: the view model and the workers only ask. Tested by
 * WellnessRulesTest and WellnessPermissionUxTest.
 *
 * The box owns the truth about enrollment state, consent and providers; the phone owns the
 * truth about Health Connect (availability, grants, the outbox). These rules merge the two
 * into what the screen shows and whether a sync may run.
 */
object WellnessRules {

    // ── states ─────────────────────────────────────────────────────────────────────────────

    /** The enrollment states of the wire contract, plus NOT_SET_UP for "no record at all". */
    enum class EnrollmentState(val wire: String) {
        NOT_SET_UP("not_set_up"),
        AWAITING_PHONE("awaiting_phone"),
        ACTIVE("active"),
        PAUSED("paused"),
        STALE("stale"),
        NEEDS_ATTENTION("needs_attention"),
        OWNERSHIP_CHANGED("ownership_changed"),
        REVOKED("revoked"),
        /**
         * This phone asked to replace a health source that is still online: it holds a key,
         * but the box waits for the OLD phone's approval (`POST /wellness/replace-approve`)
         * before it accepts anything from it. The enroll answer's state; the box's status never
         * says it (to anyone but the old phone, it is the old enrollment's state).
         */
        REPLACE_PENDING("replace_pending"),
        ;

        companion object {
            fun fromWire(id: String?): EnrollmentState = entries.firstOrNull { it.wire == id } ?: NOT_SET_UP
        }
    }

    /** The status chip at the top of Settings, Health (spec §6.1). */
    enum class StatusChip(val label: String) {
        NOT_SET_UP("Not set up"),
        ACTIVE("Active"),
        NEEDS_ATTENTION("Needs attention"),
        PAUSED("Paused"),
    }

    /**
     * The chip for [state] as the box reports it, the phone's own binding, and the merged
     * trouble list. A phone that holds no binding is "Not set up" whatever the box says: the
     * collector cannot run without K.
     */
    fun statusChip(state: EnrollmentState, boundHere: Boolean, troubles: List<String>, held: Boolean = false): StatusChip = when {
        !boundHere -> StatusChip.NOT_SET_UP
        state == EnrollmentState.NOT_SET_UP || state == EnrollmentState.AWAITING_PHONE ||
            state == EnrollmentState.REVOKED -> StatusChip.NOT_SET_UP
        // A Pause / Disconnect / erase asked for here and not yet confirmed by the box: the
        // phone has already stopped, so it says so.
        held || state == EnrollmentState.PAUSED -> StatusChip.PAUSED
        state == EnrollmentState.NEEDS_ATTENTION || state == EnrollmentState.OWNERSHIP_CHANGED ||
            state == EnrollmentState.STALE || state == EnrollmentState.REPLACE_PENDING -> StatusChip.NEEDS_ATTENTION
        troubles.isNotEmpty() -> StatusChip.NEEDS_ATTENTION
        else -> StatusChip.ACTIVE
    }

    // ── trouble cards ──────────────────────────────────────────────────────────────────────

    /** What a trouble card's button does. The screen maps each to an intent. */
    enum class TroubleFix(val label: String) {
        OPEN_HEALTH_CONNECT("Open Health Connect permissions"),
        UPDATE_HEALTH_CONNECT("Get Health Connect"),
        OPEN_SAMSUNG_HEALTH("Open Samsung Health"),
        OPEN_BATTERY_SETTINGS("Open battery settings"),
        RECONNECT("Reconnect Health"),
        SYNC_NOW("Sync now"),
        NONE(""),
    }

    data class TroubleCard(val code: String, val title: String, val body: String, val fix: TroubleFix)

    const val PERMISSION_REVOKED_PREFIX = "permission_revoked:"

    /**
     * Fixed user wording per trouble code (box codes from spec §3.5 plus the phone's own).
     * Calm and factual: a trouble is a data-quality state, never a body signal.
     * Unknown codes get null (shown nowhere) so a newer box can add codes safely.
     */
    fun troubleCard(code: String): TroubleCard? {
        if (code.startsWith(PERMISSION_REVOKED_PREFIX)) {
            val cat = WellnessCategory.fromWire(code.removePrefix(PERMISSION_REVOKED_PREFIX))
            val name = cat?.label ?: "Some health data"
            return TroubleCard(
                code, "$name access is off",
                "Health Connect no longer lets BlackBox read ${name.lowercase()}. What was already stored stays until you erase it.",
                TroubleFix.OPEN_HEALTH_CONNECT,
            )
        }
        return when (code) {
            "stale_sync" -> TroubleCard(code, "Your data hasn't synced lately",
                "BlackBox answers from what it has and says how old it is.", TroubleFix.SYNC_NOW)
            "not_worn_likely" -> TroubleCard(code, "Your watch may not have been worn",
                "Some recent nights or days have little heart-rate data, so they are left out of averages.",
                TroubleFix.NONE)
            "hr_mode_manual" -> TroubleCard(code, "Heart rate is only measured by hand",
                "Sleeping heart rate needs continuous or 10-minute measurement on your watch.",
                TroubleFix.OPEN_SAMSUNG_HEALTH)
            "history_window_capped" -> TroubleCard(code, "Only the last 30 days are readable",
                "Allow past data in Health Connect to bring in your older history.", TroubleFix.OPEN_HEALTH_CONNECT)
            "foreground_only" -> TroubleCard(code, "Syncs when you open BlackBox",
                "Background access is off, so health data is read only while the app is open.",
                TroubleFix.OPEN_HEALTH_CONNECT)
            "battery_restricted" -> TroubleCard(code, "Battery settings delay syncing",
                "Android is holding back the hourly sync. Letting BlackBox run unrestricted keeps it on time.",
                TroubleFix.OPEN_BATTERY_SETTINGS)
            "source_unreachable" -> TroubleCard(code, "The box can't reach this phone",
                "Pushes from the phone still work; the box's sync requests are not getting through.", TroubleFix.SYNC_NOW)
            "ownership_changed" -> TroubleCard(code, "This phone was given to someone else",
                "In Devices this phone no longer belongs to you, so health syncing has stopped.", TroubleFix.NONE)
            "hc_unavailable" -> TroubleCard(code, "Health Connect isn't available",
                "This phone has no Health Connect, which BlackBox reads health data from.", TroubleFix.UPDATE_HEALTH_CONNECT)
            "hc_needs_update" -> TroubleCard(code, "Health Connect needs an update",
                "Update Health Connect so BlackBox can read your data.", TroubleFix.UPDATE_HEALTH_CONNECT)
            "reconnect" -> TroubleCard(code, "Reconnect Health",
                "The box didn't accept this phone's key. Set Health up again on this phone.", TroubleFix.RECONNECT)
            "outbox_full" -> TroubleCard(code, "Waiting to reach the box",
                "Health data is waiting on this phone. Health Connect still has it, so nothing is lost.",
                TroubleFix.SYNC_NOW)
            "samsung_not_writing" -> TroubleCard(code, "Samsung Health isn't sharing yet",
                "In Samsung Health, Settings, Health Connect: allow Sleep, Heart rate, Steps, Exercise (and Blood pressure).",
                TroubleFix.OPEN_SAMSUNG_HEALTH)
            REPLACE_PENDING_TROUBLE -> TroubleCard(code, "Waiting for your other phone",
                "On the phone that is your health source now, open BlackBox, Settings, Health and approve this one. " +
                    "Until then the box takes nothing from this phone.", TroubleFix.NONE)
            else -> null
        }
    }

    /** Phone-side facts that produce trouble codes. */
    data class LocalFacts(
        val sdk: HcSdkStatus,
        val consentedCategories: Set<WellnessCategory>,
        val granted: Set<String>,
        val backgroundGranted: Boolean,
        val needsReconnect: Boolean = false,
        val outboxFull: Boolean = false,
        val samsungInstalled: Boolean = false,
        val samsungWritesToHc: Boolean = true,
        val batteryExempt: Boolean = true,
        val periodicOverdue: Boolean = false,
        /** This phone's enrollment waits for the old phone's approval ([EnrollmentState.REPLACE_PENDING]). */
        val replacePending: Boolean = false,
    )

    const val REPLACE_PENDING_TROUBLE = "replace_pending"

    /** The phone's own trouble codes, in display order. */
    fun localTroubles(f: LocalFacts): List<String> {
        val out = mutableListOf<String>()
        if (f.replacePending) out += REPLACE_PENDING_TROUBLE
        when (f.sdk) {
            HcSdkStatus.UNAVAILABLE -> out += "hc_unavailable"
            HcSdkStatus.NEEDS_UPDATE -> out += "hc_needs_update"
            HcSdkStatus.AVAILABLE -> Unit
        }
        if (f.needsReconnect) out += "reconnect"
        if (f.sdk == HcSdkStatus.AVAILABLE) {
            for (cat in WellnessCategory.entries) {
                if (cat !in f.consentedCategories) continue
                val needed = WellnessCatalog.readPermissionsFor(listOf(cat))
                if (needed.none { it in f.granted }) out += PERMISSION_REVOKED_PREFIX + cat.wire
            }
            if (!f.backgroundGranted) out += "foreground_only"
            else if (!f.batteryExempt && f.periodicOverdue) out += "battery_restricted"
            if (f.samsungInstalled && !f.samsungWritesToHc) out += "samsung_not_writing"
        }
        if (f.outboxFull) out += "outbox_full"
        return out
    }

    /** Box troubles first (they carry data-quality states), then the phone's; no repeats. */
    fun mergeTroubles(box: List<String>, local: List<String>): List<String> =
        (box + local).filter { troubleCard(it) != null }.distinct()

    // ── sync gating ────────────────────────────────────────────────────────────────────────

    sealed class SyncDecision {
        /** Run. [collect] false = only drain the outbox (at the cap tokens must not advance). */
        data class Run(val collect: Boolean) : SyncDecision()
        data class Skip(val reason: String) : SyncDecision()
    }

    data class SyncInputs(
        val bound: Boolean,
        val state: EnrollmentState,
        val sdk: HcSdkStatus,
        val grantedReadTypes: Int,
        val inForeground: Boolean,
        val backgroundGranted: Boolean,
        val stopped: Boolean,
        val outboxFull: Boolean,
        val backoffUntilMs: Long,
        val nowMs: Long,
        /** A narrowing asked for on this phone that the box has not confirmed ([WellnessSyncState.localHold]). */
        val held: Boolean = false,
    )

    /**
     * May a sync (or a history-import page) run now? Foreground-only mode (no background
     * grant) reads ONLY while the app is visible, decided when the work RUNS: the worker never
     * borrows another foreground service, or a "foreground" flag set when it was queued, to
     * read without the grant (spec §3.2, decision D1).
     */
    fun maySync(i: SyncInputs): SyncDecision = when {
        !i.bound -> SyncDecision.Skip("not_enrolled")
        i.stopped -> SyncDecision.Skip("stopped")
        i.held -> SyncDecision.Skip("held")
        i.state == EnrollmentState.PAUSED -> SyncDecision.Skip("paused")
        i.state == EnrollmentState.REVOKED || i.state == EnrollmentState.OWNERSHIP_CHANGED ->
            SyncDecision.Skip(i.state.wire)
        i.sdk != HcSdkStatus.AVAILABLE -> SyncDecision.Skip("hc_unavailable")
        i.nowMs < i.backoffUntilMs -> SyncDecision.Skip("backoff")
        i.grantedReadTypes == 0 -> SyncDecision.Skip("no_permissions")
        !i.inForeground && !i.backgroundGranted -> SyncDecision.Skip("foreground_only")
        else -> SyncDecision.Run(collect = !i.outboxFull)
    }

    /**
     * The skips that only forbid READING Health Connect. What is already in the outbox was read
     * with consent and may still go to the box (each send is filtered by the consent of that
     * moment). Every other skip (stopped, held, paused, revoked, backoff) is a full stop.
     */
    fun mayDrainWhenSkipped(reason: String): Boolean =
        reason == "foreground_only" || reason == "no_permissions" || reason == "hc_unavailable"

    /**
     * Should the app start (or resume) the history import? Not once it is done, and never while
     * the box refused the key, a narrowing is held, or the enrollment is paused or over: the
     * import would only read Health Connect into an outbox nobody may receive.
     */
    fun shouldStartBackfill(done: Boolean, needsReconnect: Boolean, held: Boolean, state: EnrollmentState): Boolean =
        !done && !needsReconnect && !held && state != EnrollmentState.PAUSED &&
            state != EnrollmentState.REVOKED && state != EnrollmentState.OWNERSHIP_CHANGED &&
            // Waiting for the old phone's approval: the box takes nothing yet, and a decline
            // revokes this key. The import starts on the first app open after the approval.
            state != EnrollmentState.REPLACE_PENDING

    /** What the collector does with the box's answer to its signed status read. */
    enum class StatusReaction {
        /** 200: take the box's consent and state as the truth. */
        APPLY,
        /** 401: the key is refused; stop and show "Reconnect Health" (as for a push). */
        STOP_RECONNECT,
        /** 409 / 410: owner changed, source moved or revoked; stop and clear (as for a push). */
        STOP_AND_CLEAR,
        /** 503 "paused": the person paused Health elsewhere. */
        PAUSED,
        /** No answer or anything else: carry on with the last view the phone has. */
        KEEP_LOCAL,
    }

    fun statusReaction(status: Int, error: String?): StatusReaction = when {
        status == 200 -> StatusReaction.APPLY
        status == 401 -> StatusReaction.STOP_RECONNECT
        status == 409 || status == 410 -> StatusReaction.STOP_AND_CLEAR
        status == 503 && error == "paused" -> StatusReaction.PAUSED
        else -> StatusReaction.KEEP_LOCAL
    }

    // ── narrowing asked for on this phone (Pause, Disconnect, Stop & erase) ───────────────

    const val HOLD_PAUSE = "pause"
    const val HOLD_DISCONNECT = "disconnect"
    const val HOLD_ERASE = "erase"

    /** What the phone does once the box has answered a narrowing it already applied locally. */
    enum class NarrowingOutcome {
        /** 2xx: the box agrees; drop the hold (Pause) or forget everything (Disconnect, erase). */
        CONFIRMED,
        /** 401 / 409 / 410: the box no longer accepts this key; forget everything here. */
        KEY_REFUSED,
        /** No answer or another refusal: stay stopped (the hold stays) and let the person retry. */
        UNCONFIRMED,
    }

    fun narrowingOutcome(status: Int): NarrowingOutcome = when (status) {
        in 200..299 -> NarrowingOutcome.CONFIRMED
        401, 409, 410 -> NarrowingOutcome.KEY_REFUSED
        else -> NarrowingOutcome.UNCONFIRMED
    }

    // ── moving Health to another phone (spec §3.1; `POST /wellness/replace-approve`) ──────

    /**
     * The OLD phone's approval card: another phone of this operator asked to become the
     * health source while this one is online, and the box holds it until this phone answers
     * (`replace_pending` in this phone's signed status; nobody else's status carries it).
     */
    fun showReplaceApproval(boundHere: Boolean, credentialed: Boolean, replacePending: Boolean?): Boolean =
        boundHere && credentialed && replacePending == true

    /** Did an enrollment end waiting for the old phone (the enroll answer's `state`)? */
    fun awaitsApproval(enrollState: String?): Boolean = enrollState == EnrollmentState.REPLACE_PENDING.wire

    /**
     * The state the Health screen shows. A phone waiting for approval reads only the box's
     * four uncredentialed fields, whose state is the OLD enrollment's; until its own signed
     * status is credentialed (it was approved), the phone's REPLACE_PENDING stands.
     */
    fun shownState(box: EnrollmentState?, boxCredentialed: Boolean, local: EnrollmentState): EnrollmentState = when {
        local == EnrollmentState.REPLACE_PENDING && !boxCredentialed -> local
        else -> box ?: local
    }

    const val ENROLLED_MESSAGE = "This phone is now your health source."
    const val AWAITING_APPROVAL_MESSAGE = "Almost done: on the phone that is your health source now, open BlackBox, " +
        "Settings, Health and approve this one. Your history and AI choices move with it."

    /** What the old phone does after its answer to a replacement. */
    enum class ReplaceOutcome(val message: String) {
        /** Approved: the box moved Health to the new phone and revoked this one's key; forget Health here. */
        MOVED("Health moved to your new phone. This phone no longer syncs health data."),
        /** Declined: this phone stays the source; the other phone's key was revoked. */
        KEPT("This phone stays your health source. The other phone's request was turned down."),
        /** The request expired (24 h) or was answered already. */
        NOTHING_PENDING("There's no request to move Health any more."),
        /** The box no longer accepts this phone at all: forget Health here. */
        KEY_REFUSED("Your box no longer accepts this phone for Health, so Health was removed from it."),
        /** No answer, or a refusal that changes nothing: the request still waits. */
        UNCONFIRMED("Couldn't get an answer from your box; try again."),
    }

    fun replaceOutcome(status: Int, error: String?, approve: Boolean): ReplaceOutcome = when {
        status in 200..299 -> if (approve) ReplaceOutcome.MOVED else ReplaceOutcome.KEPT
        status == 409 && error == "no_pending_replace" -> ReplaceOutcome.NOTHING_PENDING
        // 403: the box's answer when this phone is no longer the operator's active source.
        status == 401 || status == 403 || status == 409 || status == 410 -> ReplaceOutcome.KEY_REFUSED
        else -> ReplaceOutcome.UNCONFIRMED
    }

    /** Does this outcome end Health on this phone? */
    fun replaceClearsHere(outcome: ReplaceOutcome): Boolean =
        outcome == ReplaceOutcome.MOVED || outcome == ReplaceOutcome.KEY_REFUSED

    /** Foreground runs are debounced: at most one expedited sync per 10 minutes of app opens. */
    const val FOREGROUND_DEBOUNCE_MS = 10 * 60_000L

    fun shouldSyncOnForeground(lastForegroundSyncMs: Long, nowMs: Long): Boolean =
        lastForegroundSyncMs <= 0L || nowMs - lastForegroundSyncMs >= FOREGROUND_DEBOUNCE_MS ||
            nowMs < lastForegroundSyncMs

    const val INTERVAL_MIN = 15
    const val INTERVAL_MAX = 120
    const val INTERVAL_DEFAULT = 60
    const val INTERVAL_FLEX = 15

    fun clampInterval(minutes: Int): Int = minutes.coerceIn(INTERVAL_MIN, INTERVAL_MAX)

    /** The periodic worker is "overdue" after three missed intervals (a T-9 deferral signal). */
    fun periodicOverdue(lastPeriodicRunMs: Long, intervalMin: Int, nowMs: Long): Boolean =
        lastPeriodicRunMs > 0L && nowMs - lastPeriodicRunMs > 3L * clampInterval(intervalMin) * 60_000L

    /** Quota back-off: 1 minute doubling to a 2-hour ceiling. */
    fun quotaBackoffMs(attempt: Int): Long {
        val base = 60_000L
        val cap = 2 * 60 * 60_000L
        if (attempt >= 7) return cap
        return minOf(cap, base shl attempt.coerceAtLeast(0))
    }

    // ── the outbox response table (spec §3.2) ─────────────────────────────────────────────

    enum class OutboxReaction {
        /** 200: delete the batch. */
        DELETE,
        /** 401: stop and show "Reconnect Health". */
        STOP_RECONNECT,
        /** 409 / 410: stop and clear (owner changed, source moved, revoked). */
        STOP_AND_CLEAR,
        /** 413: split the batch in two and retry the halves. */
        SPLIT,
        /** 422: set it aside (counted), carry on with the next. */
        QUARANTINE,
        /** 429 / 503: back off, keep the batch. */
        BACK_OFF,
        /** Network failure or anything else: keep it for the next run. */
        RETRY_LATER,
    }

    fun outboxReaction(status: Int): OutboxReaction = when (status) {
        200 -> OutboxReaction.DELETE
        401 -> OutboxReaction.STOP_RECONNECT
        409, 410 -> OutboxReaction.STOP_AND_CLEAR
        413 -> OutboxReaction.SPLIT
        422 -> OutboxReaction.QUARANTINE
        429, 503 -> OutboxReaction.BACK_OFF
        else -> OutboxReaction.RETRY_LATER
    }

    // ── permissions (WellnessPermissionUxTest) ─────────────────────────────────────────────

    /** The past-data card (setup step 5) appears only when Health Connect says it can grant it. */
    fun showPastDataCard(history: HcFeatureStatus): Boolean = history == HcFeatureStatus.AVAILABLE

    /** The background card likewise. Neither card is ever pre-selected. */
    fun showBackgroundCard(background: HcFeatureStatus): Boolean = background == HcFeatureStatus.AVAILABLE

    /**
     * The exact set handed to the Health Connect dialog: READ permissions for the categories
     * the user turned on and nothing else, plus past data and background only when the user
     * ticked them AND the feature is available. Never a write, never the exercise route.
     */
    fun permissionsToRequest(
        consent: Map<WellnessCategory, WellnessConsent>,
        wantHistory: Boolean,
        wantBackground: Boolean,
        history: HcFeatureStatus,
        background: HcFeatureStatus,
    ): Set<String> {
        val on = consent.filterValues { it.isOn }.keys
        val out = linkedSetOf<String>()
        out += WellnessCatalog.readPermissionsFor(on)
        if (out.isEmpty()) return out
        if (wantHistory && showPastDataCard(history)) out += WellnessCatalog.PERMISSION_HISTORY
        if (wantBackground && showBackgroundCard(background)) out += WellnessCatalog.PERMISSION_BACKGROUND
        return out
    }

    /**
     * The M7.1 probe's "ask for everything" (debug or `-PwellnessProbe` builds only): every
     * category's READ permissions, plus past data and background only where Health Connect
     * says it can grant them, the same availability rule as setup.
     */
    fun probePermissions(history: HcFeatureStatus, background: HcFeatureStatus): Set<String> =
        permissionsToRequest(WellnessCategory.entries.associateWith { WellnessConsent.ASK },
            wantHistory = true, wantBackground = true, history = history, background = background)

    /** Record types the collector may read: consented AND granted. */
    fun readableTypes(consent: Map<WellnessCategory, WellnessConsent>, granted: Set<String>): List<HcType> =
        WellnessCatalog.typesFor(consent.filterValues { it.isOn }.keys).filter { it.readPermission in granted }

    // ── the setup flow (spec §6.1, nine steps) ────────────────────────────────────────────

    enum class SetupStep(val title: String) {
        WHAT_THIS_IS("What this is"),
        THIS_PHONE("This phone"),
        WHAT_TO_SHARE("What to share"),
        YOUR_PRIVACY("Your privacy"),
        PAST_AND_BACKGROUND("Past data and background access"),
        WHICH_AI("Which AI can see it"),
        WHERE_YOU_CAN_ASK("Where you can ask"),
        MORNING_BRIEFING("Morning briefing"),
        DONE("Done"),
    }

    data class SetupState(
        val attested: Boolean = false,
        val sdk: HcSdkStatus = HcSdkStatus.UNAVAILABLE,
        val paired: Boolean = false,
        val operator: String = "",
        val consent: Map<WellnessCategory, WellnessConsent> = WellnessCatalog.DEFAULT_CONSENT,
        val granted: Set<String> = emptySet(),
        val enrolled: Boolean = false,
    )

    /** Why the user cannot leave [step] yet, or null when Next is allowed. */
    fun blockerFor(step: SetupStep, s: SetupState): String? = when (step) {
        SetupStep.WHAT_THIS_IS -> if (!s.attested) "Confirm that this is your own data first." else null
        SetupStep.THIS_PHONE -> when {
            s.operator.isBlank() -> "Pick your operator in Settings first."
            s.sdk == HcSdkStatus.UNAVAILABLE -> "This phone has no Health Connect."
            s.sdk == HcSdkStatus.NEEDS_UPDATE -> "Update Health Connect first."
            !s.paired -> "Open BlackBox once while connected to your box so it can pair."
            else -> null
        }
        SetupStep.WHAT_TO_SHARE -> if (s.consent.values.none { it.isOn }) "Turn on at least one kind of data." else null
        SetupStep.PAST_AND_BACKGROUND ->
            if (WellnessCatalog.readPermissionsFor(s.consent.filterValues { it.isOn }.keys).none { it in s.granted })
                "Allow at least one kind of data in Health Connect." else null
        SetupStep.WHERE_YOU_CAN_ASK -> null
        SetupStep.DONE -> if (!s.enrolled) "Setup hasn't finished." else null
        else -> null
    }

    fun nextStep(step: SetupStep): SetupStep? = SetupStep.entries.getOrNull(step.ordinal + 1)

    fun previousStep(step: SetupStep): SetupStep? = SetupStep.entries.getOrNull(step.ordinal - 1)

    // ── settings ───────────────────────────────────────────────────────────────────────────

    const val INSTINCT_PROMPT_MAX = 2000

    /**
     * The instinct prompt as the box will store it: control characters removed (newlines and
     * tabs kept, they are how people write prompts), capped at [INSTINCT_PROMPT_MAX].
     */
    fun sanitizeInstinctPrompt(text: String): String {
        val cleaned = buildString(text.length) {
            for (ch in text) {
                if (ch == '\n' || ch == '\t' || !(ch.isISOControl() || ch == ' ' || ch == ' ')) append(ch)
            }
        }
        return if (cleaned.length <= INSTINCT_PROMPT_MAX) cleaned else cleaned.take(INSTINCT_PROMPT_MAX)
    }

    /** The default instinct prompt (spec §10); null in the record means "use this". */
    const val DEFAULT_INSTINCT_PROMPT =
        "You have this person's own wearable data through the wellness_* tools. When a question touches " +
            "sleep, energy, recovery, training, stress, travel, illness, or planning the day, check the data " +
            "first and bring in what is relevant, with coverage and 'as of'. Use their own usual range for " +
            "comparisons, keep it short, and don't bring health up when they ask about something else."

    /** Settings as the credentialed status reports them (the fields `PUT /wellness/settings` takes). */
    data class Settings(
        val categories: Map<WellnessCategory, WellnessConsent> = WellnessCatalog.DEFAULT_CONSENT,
        val localOnly: Map<WellnessCategory, Boolean> = WellnessCatalog.DEFAULT_LOCAL_ONLY,
        val providersAllowed: Set<String> = emptySet(),
        val googlePaidKeyConfirmed: Boolean = false,
        val surfacesMode: String = "auto",
        val instinctPrompt: String? = null,
    )

    /**
     * Does going from [old] to [new] WIDEN access (rule W21)? A category from off to on, a
     * provider added, local-only turned off, Google's paid-key tick, or surfaces to household.
     * The phone signs every settings call anyway; this tells the UI which edits only the
     * enrolled phone can make, so the screen can say so on another surface.
     */
    fun widens(old: Settings, new: Settings): Boolean {
        for (cat in WellnessCategory.entries) {
            val was = old.categories[cat] ?: WellnessConsent.OFF
            val now = new.categories[cat] ?: WellnessConsent.OFF
            if (!was.isOn && now.isOn) return true
            if ((old.localOnly[cat] ?: true) && !(new.localOnly[cat] ?: true)) return true
        }
        if ((new.providersAllowed - old.providersAllowed).isNotEmpty()) return true
        if (!old.googlePaidKeyConfirmed && new.googlePaidKeyConfirmed) return true
        if (old.surfacesMode != "household" && new.surfacesMode == "household") return true
        return false
    }

    /** "Stop & erase" needs the word typed out. */
    const val ERASE_WORD = "erase"

    fun eraseConfirmed(typed: String): Boolean = typed.trim().equals(ERASE_WORD, ignoreCase = true)

    /** Disconnect has no default: the user must pick Keep or Delete history. */
    fun disconnectAllowed(keepHistoryChoice: Boolean?): Boolean = keepHistoryChoice != null

    // ── sources ────────────────────────────────────────────────────────────────────────────

    /**
     * Heart-rate mode from the median gap between samples (spec §3.5): about every minute is
     * continuous, about every 10 minutes is every_10_min, anything sparser is manual. Fewer
     * than [MIN_SAMPLES_FOR_MODE] samples is unknown.
     */
    fun hrMode(medianGapS: Long?, samples: Int): String = when {
        medianGapS == null || samples < MIN_SAMPLES_FOR_MODE -> "unknown"
        medianGapS <= 180 -> "continuous"
        medianGapS <= 1800 -> "every_10_min"
        else -> "manual"
    }

    const val MIN_SAMPLES_FOR_MODE = 24

    /**
     * The "auto" heart-rate origin: the wearable origin with the most records over the probe
     * window; failing a wearable, the origin with the most records. Ties break by package so
     * the choice is deterministic. Null when there is nothing.
     */
    fun autoOrigin(counts: Map<HcOrigin, Int>): String? {
        val byPackage = counts.entries.groupBy { it.key.packageName }
            .mapValues { (_, list) -> list.sumOf { it.value } to list.any { it.key.deviceType in HcOrigin.WEARABLE_TYPES } }
            .filter { it.value.first > 0 }
        if (byPackage.isEmpty()) return null
        val pool = byPackage.filter { it.value.second }.ifEmpty { byPackage }
        return pool.entries.sortedWith(compareByDescending<Map.Entry<String, Pair<Int, Boolean>>> { it.value.first }
            .thenBy { it.key }).first().key
    }

    /** "Samsung Health, a watch" / "Samsung Health, this phone" / "Oura" (spec §3.1 wording). */
    fun originLabel(appLabel: String, deviceType: String?, model: String?, isThisPhone: Boolean = false): String {
        val device = when {
            isThisPhone || deviceType == "PHONE" -> "this phone"
            deviceType == "WATCH" -> if (model != null) "watch $model" else "a watch"
            deviceType == "RING" -> if (model != null) "ring $model" else "a ring"
            deviceType == "FITNESS_BAND" -> "a fitness band"
            deviceType == "CHEST_STRAP" -> "a chest strap"
            deviceType == "SCALE" -> "a scale"
            else -> null
        }
        return if (device == null) appLabel else "$appLabel, $device"
    }
}
