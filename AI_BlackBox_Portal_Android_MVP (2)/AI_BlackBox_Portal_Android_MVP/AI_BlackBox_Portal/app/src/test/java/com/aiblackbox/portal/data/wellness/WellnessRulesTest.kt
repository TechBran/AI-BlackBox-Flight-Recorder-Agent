package com.aiblackbox.portal.data.wellness

import com.aiblackbox.portal.data.wellness.WellnessRules.EnrollmentState
import com.aiblackbox.portal.data.wellness.WellnessRules.OutboxReaction
import com.aiblackbox.portal.data.wellness.WellnessRules.StatusChip
import com.aiblackbox.portal.data.wellness.WellnessRules.SyncDecision
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class WellnessRulesTest {

    // ── status chip ────────────────────────────────────────────────────────────────────────

    @Test
    fun `chip is not set up without a local binding whatever the box says`() {
        assertEquals(StatusChip.NOT_SET_UP, WellnessRules.statusChip(EnrollmentState.ACTIVE, boundHere = false, troubles = emptyList()))
        assertEquals(StatusChip.NOT_SET_UP, WellnessRules.statusChip(EnrollmentState.AWAITING_PHONE, true, emptyList()))
        assertEquals(StatusChip.NOT_SET_UP, WellnessRules.statusChip(EnrollmentState.REVOKED, true, emptyList()))
    }

    @Test
    fun `chip follows state and troubles`() {
        assertEquals(StatusChip.ACTIVE, WellnessRules.statusChip(EnrollmentState.ACTIVE, true, emptyList()))
        assertEquals(StatusChip.PAUSED, WellnessRules.statusChip(EnrollmentState.PAUSED, true, listOf("stale_sync")))
        assertEquals(StatusChip.NEEDS_ATTENTION, WellnessRules.statusChip(EnrollmentState.ACTIVE, true, listOf("foreground_only")))
        for (s in listOf(EnrollmentState.STALE, EnrollmentState.NEEDS_ATTENTION, EnrollmentState.OWNERSHIP_CHANGED)) {
            assertEquals(StatusChip.NEEDS_ATTENTION, WellnessRules.statusChip(s, true, emptyList()))
        }
    }

    @Test
    fun `enrollment state parses the contract ids and defaults to not set up`() {
        for (s in EnrollmentState.entries) assertEquals(s, EnrollmentState.fromWire(s.wire))
        assertEquals(EnrollmentState.NOT_SET_UP, EnrollmentState.fromWire("bogus"))
        assertEquals(EnrollmentState.NOT_SET_UP, EnrollmentState.fromWire(null))
    }

    // ── trouble cards ──────────────────────────────────────────────────────────────────────

    @Test
    fun `every spec trouble code has calm fixed wording and a fix`() {
        val codes = listOf(
            "stale_sync", "not_worn_likely", "hr_mode_manual", "history_window_capped", "permission_revoked:sleep",
            "foreground_only", "battery_restricted", "source_unreachable", "ownership_changed",
            "hc_unavailable", "hc_needs_update", "reconnect", "outbox_full", "samsung_not_writing",
        )
        val banned = listOf("dangerous", "alarming", "critical", "warning", "abnormal")
        for (code in codes) {
            val card = WellnessRules.troubleCard(code)
            assertNotNull(code, card)
            val text = (card!!.title + " " + card.body).lowercase()
            for (word in banned) assertFalse("$code says '$word'", word in text)
            // Product wording ("10-minute", "30 days") is fine; a health unit would mean a value.
            for (unit in listOf("bpm", "mmhg", "%", "kcal")) assertFalse("$code names '$unit'", unit in text)
        }
        assertEquals(WellnessRules.TroubleFix.OPEN_BATTERY_SETTINGS, WellnessRules.troubleCard("battery_restricted")!!.fix)
        assertEquals(WellnessRules.TroubleFix.RECONNECT, WellnessRules.troubleCard("reconnect")!!.fix)
        assertTrue(WellnessRules.troubleCard("permission_revoked:blood_pressure")!!.title.startsWith("Blood pressure"))
        assertNull(WellnessRules.troubleCard("something_new"))
    }

    @Test
    fun `local troubles come from the phone's own facts`() {
        val granted = WellnessCatalog.readPermissionsFor(listOf(WellnessCategory.ACTIVITY)).toSet()
        val troubles = WellnessRules.localTroubles(
            WellnessRules.LocalFacts(
                sdk = HcSdkStatus.AVAILABLE,
                consentedCategories = setOf(WellnessCategory.SLEEP, WellnessCategory.ACTIVITY),
                granted = granted,
                backgroundGranted = false,
                samsungInstalled = true,
                samsungWritesToHc = false,
            ),
        )
        assertEquals(listOf("permission_revoked:sleep", "foreground_only", "samsung_not_writing"), troubles)
        assertEquals(
            listOf("hc_needs_update"),
            WellnessRules.localTroubles(WellnessRules.LocalFacts(HcSdkStatus.NEEDS_UPDATE, setOf(WellnessCategory.SLEEP), emptySet(), false)),
        )
        val battery = WellnessRules.localTroubles(
            WellnessRules.LocalFacts(HcSdkStatus.AVAILABLE, emptySet(), emptySet(), backgroundGranted = true,
                batteryExempt = false, periodicOverdue = true, outboxFull = true, needsReconnect = true),
        )
        assertEquals(listOf("reconnect", "battery_restricted", "outbox_full"), battery)
    }

    @Test
    fun `merged troubles keep box order first, drop repeats and unknown codes`() {
        assertEquals(
            listOf("stale_sync", "foreground_only", "reconnect"),
            WellnessRules.mergeTroubles(listOf("stale_sync", "foreground_only", "future_code"), listOf("foreground_only", "reconnect")),
        )
    }

    // ── sync gating ────────────────────────────────────────────────────────────────────────

    private fun inputs(
        bound: Boolean = true,
        state: EnrollmentState = EnrollmentState.ACTIVE,
        sdk: HcSdkStatus = HcSdkStatus.AVAILABLE,
        types: Int = 3,
        fg: Boolean = false,
        bg: Boolean = true,
        stopped: Boolean = false,
        full: Boolean = false,
        backoffUntil: Long = 0L,
        now: Long = 1_000_000L,
    ) = WellnessRules.SyncInputs(bound, state, sdk, types, fg, bg, stopped, full, backoffUntil, now)

    @Test
    fun `sync runs when everything is in place`() {
        assertEquals(SyncDecision.Run(collect = true), WellnessRules.maySync(inputs()))
        assertEquals(SyncDecision.Run(collect = true), WellnessRules.maySync(inputs(fg = true, bg = false)))
    }

    @Test
    fun `foreground-only mode never reads in the background`() {
        assertEquals(SyncDecision.Skip("foreground_only"), WellnessRules.maySync(inputs(fg = false, bg = false)))
    }

    @Test
    fun `sync refuses when unenrolled, stopped, paused, revoked, unavailable, backing off or ungranted`() {
        assertEquals("not_enrolled", (WellnessRules.maySync(inputs(bound = false)) as SyncDecision.Skip).reason)
        assertEquals("stopped", (WellnessRules.maySync(inputs(stopped = true)) as SyncDecision.Skip).reason)
        assertEquals("paused", (WellnessRules.maySync(inputs(state = EnrollmentState.PAUSED)) as SyncDecision.Skip).reason)
        assertEquals("revoked", (WellnessRules.maySync(inputs(state = EnrollmentState.REVOKED)) as SyncDecision.Skip).reason)
        assertEquals("ownership_changed",
            (WellnessRules.maySync(inputs(state = EnrollmentState.OWNERSHIP_CHANGED)) as SyncDecision.Skip).reason)
        assertEquals("hc_unavailable", (WellnessRules.maySync(inputs(sdk = HcSdkStatus.NEEDS_UPDATE)) as SyncDecision.Skip).reason)
        assertEquals("backoff", (WellnessRules.maySync(inputs(backoffUntil = 2_000_000L)) as SyncDecision.Skip).reason)
        assertEquals("no_permissions", (WellnessRules.maySync(inputs(types = 0)) as SyncDecision.Skip).reason)
    }

    @Test
    fun `a full outbox drains but does not collect`() {
        assertEquals(SyncDecision.Run(collect = false), WellnessRules.maySync(inputs(full = true)))
    }

    @Test
    fun `foreground syncs are debounced to ten minutes`() {
        assertTrue(WellnessRules.shouldSyncOnForeground(0L, 5_000L))
        assertFalse(WellnessRules.shouldSyncOnForeground(1_000_000L, 1_000_000L + 9 * 60_000L))
        assertTrue(WellnessRules.shouldSyncOnForeground(1_000_000L, 1_000_000L + 10 * 60_000L))
        // A clock that went backwards does not wedge the debounce.
        assertTrue(WellnessRules.shouldSyncOnForeground(5_000_000L, 1_000_000L))
    }

    @Test
    fun `interval is clamped to 15-120 and overdue means three missed intervals`() {
        assertEquals(15, WellnessRules.clampInterval(1))
        assertEquals(120, WellnessRules.clampInterval(999))
        assertEquals(60, WellnessRules.clampInterval(60))
        assertFalse(WellnessRules.periodicOverdue(0L, 60, 10_000_000L))
        assertFalse(WellnessRules.periodicOverdue(1_000L, 60, 1_000L + 3 * 60 * 60_000L))
        assertTrue(WellnessRules.periodicOverdue(1_000L, 60, 1_000L + 3 * 60 * 60_000L + 1))
    }

    @Test
    fun `quota back-off doubles from one minute to a two-hour ceiling`() {
        assertEquals(60_000L, WellnessRules.quotaBackoffMs(0))
        assertEquals(120_000L, WellnessRules.quotaBackoffMs(1))
        assertEquals(32 * 60_000L, WellnessRules.quotaBackoffMs(5))
        assertEquals(2 * 60 * 60_000L, WellnessRules.quotaBackoffMs(7))
        assertEquals(2 * 60 * 60_000L, WellnessRules.quotaBackoffMs(40))
        assertEquals(60_000L, WellnessRules.quotaBackoffMs(-3))
    }

    @Test
    fun `the outbox response table matches the spec`() {
        assertEquals(OutboxReaction.DELETE, WellnessRules.outboxReaction(200))
        assertEquals(OutboxReaction.STOP_RECONNECT, WellnessRules.outboxReaction(401))
        assertEquals(OutboxReaction.STOP_AND_CLEAR, WellnessRules.outboxReaction(409))
        assertEquals(OutboxReaction.STOP_AND_CLEAR, WellnessRules.outboxReaction(410))
        assertEquals(OutboxReaction.SPLIT, WellnessRules.outboxReaction(413))
        assertEquals(OutboxReaction.QUARANTINE, WellnessRules.outboxReaction(422))
        assertEquals(OutboxReaction.BACK_OFF, WellnessRules.outboxReaction(429))
        assertEquals(OutboxReaction.BACK_OFF, WellnessRules.outboxReaction(503))
        assertEquals(OutboxReaction.RETRY_LATER, WellnessRules.outboxReaction(-1))
        assertEquals(OutboxReaction.RETRY_LATER, WellnessRules.outboxReaction(500))
    }

    // ── setup flow ─────────────────────────────────────────────────────────────────────────

    @Test
    fun `setup has the nine steps of the spec in order`() {
        assertEquals(9, WellnessRules.SetupStep.entries.size)
        assertEquals(WellnessRules.SetupStep.WHAT_THIS_IS, WellnessRules.SetupStep.entries.first())
        assertEquals(WellnessRules.SetupStep.DONE, WellnessRules.SetupStep.entries.last())
        assertNull(WellnessRules.nextStep(WellnessRules.SetupStep.DONE))
        assertNull(WellnessRules.previousStep(WellnessRules.SetupStep.WHAT_THIS_IS))
    }

    @Test
    fun `setup blockers - attestation, phone readiness, a category, a grant`() {
        val s = WellnessRules.SetupState()
        assertNotNull(WellnessRules.blockerFor(WellnessRules.SetupStep.WHAT_THIS_IS, s))
        assertNull(WellnessRules.blockerFor(WellnessRules.SetupStep.WHAT_THIS_IS, s.copy(attested = true)))
        assertNotNull(WellnessRules.blockerFor(WellnessRules.SetupStep.THIS_PHONE, s.copy(operator = WellnessFixtures.OPERATOR)))
        val ready = s.copy(operator = WellnessFixtures.OPERATOR, sdk = HcSdkStatus.AVAILABLE, paired = true)
        assertNull(WellnessRules.blockerFor(WellnessRules.SetupStep.THIS_PHONE, ready))
        assertNotNull(WellnessRules.blockerFor(WellnessRules.SetupStep.THIS_PHONE, ready.copy(paired = false)))
        val none = WellnessCategory.entries.associateWith { WellnessConsent.OFF }
        assertNotNull(WellnessRules.blockerFor(WellnessRules.SetupStep.WHAT_TO_SHARE, ready.copy(consent = none)))
        assertNotNull(WellnessRules.blockerFor(WellnessRules.SetupStep.PAST_AND_BACKGROUND, ready))
        assertNull(WellnessRules.blockerFor(WellnessRules.SetupStep.PAST_AND_BACKGROUND,
            ready.copy(granted = setOf(HcType.SLEEP.readPermission))))
    }

    // ── settings ───────────────────────────────────────────────────────────────────────────

    @Test
    fun `instinct prompt drops control characters, keeps newlines, caps at 2000`() {
        assertEquals("line one\nline two\ttab", WellnessRules.sanitizeInstinctPrompt("line one\u0000\nline two\u0007\ttab\u001b"))
        assertEquals(2000, WellnessRules.sanitizeInstinctPrompt("x".repeat(2500)).length)
        assertEquals("a b", WellnessRules.sanitizeInstinctPrompt("a  b"))
        assertTrue(WellnessRules.DEFAULT_INSTINCT_PROMPT.length <= WellnessRules.INSTINCT_PROMPT_MAX)
    }

    @Test
    fun `widening is a category on, a provider added, local-only off, a paid-key tick or household`() {
        val base = WellnessRules.Settings()
        val off = base.copy(categories = base.categories + (WellnessCategory.SLEEP to WellnessConsent.OFF))
        assertTrue(WellnessRules.widens(off, base))
        assertFalse(WellnessRules.widens(base, off))
        assertTrue(WellnessRules.widens(base, base.copy(providersAllowed = setOf("anthropic"))))
        assertFalse(WellnessRules.widens(base.copy(providersAllowed = setOf("anthropic")), base))
        assertTrue(WellnessRules.widens(base, base.copy(localOnly = base.localOnly + (WellnessCategory.BLOOD_PRESSURE to false))))
        assertFalse(WellnessRules.widens(base, base.copy(localOnly = base.localOnly + (WellnessCategory.SLEEP to true))))
        assertTrue(WellnessRules.widens(base, base.copy(googlePaidKeyConfirmed = true)))
        assertTrue(WellnessRules.widens(base, base.copy(surfacesMode = "household")))
        assertFalse(WellnessRules.widens(base, base.copy(surfacesMode = "credentialed")))
        // The instinct prompt is prompt text, not access.
        assertFalse(WellnessRules.widens(base, base.copy(instinctPrompt = "Check my sleep first.")))
    }

    @Test
    fun `erase needs the typed word and disconnect needs an explicit history choice`() {
        assertFalse(WellnessRules.eraseConfirmed(""))
        assertFalse(WellnessRules.eraseConfirmed("yes"))
        assertTrue(WellnessRules.eraseConfirmed(" ERASE "))
        assertFalse(WellnessRules.disconnectAllowed(null))
        assertTrue(WellnessRules.disconnectAllowed(true))
        assertTrue(WellnessRules.disconnectAllowed(false))
    }

    // ── sources ────────────────────────────────────────────────────────────────────────────

    @Test
    fun `hr mode from the median gap`() {
        assertEquals("continuous", WellnessRules.hrMode(60, 500))
        assertEquals("every_10_min", WellnessRules.hrMode(600, 500))
        assertEquals("manual", WellnessRules.hrMode(7200, 500))
        assertEquals("unknown", WellnessRules.hrMode(60, 3))
        assertEquals("unknown", WellnessRules.hrMode(null, 500))
    }

    @Test
    fun `auto origin prefers the busiest wearable, then the busiest origin, deterministically`() {
        val watch = WellnessFixtures.WATCH
        val phone = HcOrigin("com.example.phoneapp", "PHONE")
        val ring = HcOrigin("com.example.ring", "RING")
        assertEquals(watch.packageName, WellnessRules.autoOrigin(mapOf(watch to 100, phone to 900)))
        assertEquals(ring.packageName, WellnessRules.autoOrigin(mapOf(watch to 100, ring to 200)))
        assertEquals(phone.packageName, WellnessRules.autoOrigin(mapOf(phone to 5)))
        assertEquals("a.pkg", WellnessRules.autoOrigin(mapOf(HcOrigin("b.pkg", "WATCH") to 10, HcOrigin("a.pkg", "WATCH") to 10)))
        assertNull(WellnessRules.autoOrigin(emptyMap()))
    }

    @Test
    fun `origin labels follow the spec wording`() {
        assertEquals("Samsung Health, a watch", WellnessRules.originLabel("Samsung Health", "WATCH", null))
        assertEquals("Samsung Health, watch SM-R960", WellnessRules.originLabel("Samsung Health", "WATCH", "SM-R960"))
        assertEquals("Samsung Health, this phone", WellnessRules.originLabel("Samsung Health", "PHONE", "SM-TEST0"))
        assertEquals("Oura", WellnessRules.originLabel("Oura", null, null))
    }
}
