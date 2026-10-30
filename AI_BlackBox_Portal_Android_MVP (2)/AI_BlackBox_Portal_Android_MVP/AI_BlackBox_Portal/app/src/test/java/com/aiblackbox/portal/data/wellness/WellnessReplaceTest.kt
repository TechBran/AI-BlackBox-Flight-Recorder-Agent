package com.aiblackbox.portal.data.wellness

import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.TimeUnit

/**
 * Moving Health to another phone (spec §3.1; box: enroll.py modes new/same/replace/fresh and
 * `POST /wellness/replace-approve`, signed by the OLD phone). When the old source is online the
 * new phone's enrollment waits (`state: replace_pending`) and the old phone is told; the old
 * phone's signed status says `replace_pending: true` and it answers approve or decline. These
 * are the phone's halves: the old phone's card, call and outcome; the new phone's waiting state.
 */
class WellnessReplaceTest {

    // ── the old phone ──────────────────────────────────────────────────────────────────────

    @Test
    fun `the old phone shows the approval card only from its own signed status`() {
        assertTrue(WellnessRules.showReplaceApproval(boundHere = true, credentialed = true, replacePending = true))
        assertFalse(WellnessRules.showReplaceApproval(boundHere = true, credentialed = true, replacePending = false))
        assertFalse(WellnessRules.showReplaceApproval(boundHere = true, credentialed = true, replacePending = null))
        assertFalse("an unsigned status never carries it", WellnessRules.showReplaceApproval(true, false, true))
        assertFalse("a phone that is no source", WellnessRules.showReplaceApproval(false, true, true))
    }

    @Test
    fun `the signed status carries replace_pending, the unsigned one does not`() {
        val signed = WellnessStatusParser.parse("""{"state":"active","enrolled":true,"source_label":"Old phone (SM-OLD)",
            "troubles":[],"categories":{"sleep":"ask"},"providers_allowed":[],"replace_pending":true}""")!!
        assertTrue(signed.credentialed)
        assertEquals(true, signed.replacePending)
        val unsigned = WellnessStatusParser.parse("""{"state":"active","enrolled":true,"source_label":"Old phone","last_sync":null}""")!!
        assertFalse(unsigned.credentialed)
        assertEquals(null, unsigned.replacePending)
    }

    @Test
    fun `the approval body is the box's ReplaceApproveBody`() {
        val o = Json.parseToJsonElement(WellnessStatusParser.replaceApproveBody(WellnessFixtures.OPERATOR, approve = true)).jsonObject
        assertEquals(setOf("operator", "approve"), o.keys)
        assertEquals(WellnessFixtures.OPERATOR, o.getValue("operator").jsonPrimitive.content)
        assertTrue(o.getValue("approve").jsonPrimitive.boolean)
        assertFalse(Json.parseToJsonElement(WellnessStatusParser.replaceApproveBody("x", false)).jsonObject
            .getValue("approve").jsonPrimitive.boolean)
    }

    @Test
    fun `the box's answers map to what the old phone does`() {
        assertEquals(WellnessRules.ReplaceOutcome.MOVED, WellnessRules.replaceOutcome(200, null, approve = true))
        assertEquals(WellnessRules.ReplaceOutcome.KEPT, WellnessRules.replaceOutcome(200, null, approve = false))
        assertEquals(WellnessRules.ReplaceOutcome.NOTHING_PENDING, WellnessRules.replaceOutcome(409, "no_pending_replace", true))
        for (s in listOf(401, 403, 410)) assertEquals("$s", WellnessRules.ReplaceOutcome.KEY_REFUSED, WellnessRules.replaceOutcome(s, null, true))
        assertEquals(WellnessRules.ReplaceOutcome.KEY_REFUSED, WellnessRules.replaceOutcome(409, "owner_changed", true))
        for (s in listOf(-1, 400, 422, 500, 503)) assertEquals("$s", WellnessRules.ReplaceOutcome.UNCONFIRMED, WellnessRules.replaceOutcome(s, null, true))
        assertTrue(WellnessRules.replaceClearsHere(WellnessRules.ReplaceOutcome.MOVED))
        assertTrue(WellnessRules.replaceClearsHere(WellnessRules.ReplaceOutcome.KEY_REFUSED))
        assertFalse(WellnessRules.replaceClearsHere(WellnessRules.ReplaceOutcome.KEPT))
        assertFalse(WellnessRules.replaceClearsHere(WellnessRules.ReplaceOutcome.NOTHING_PENDING))
        assertFalse(WellnessRules.replaceClearsHere(WellnessRules.ReplaceOutcome.UNCONFIRMED))
    }

    @Test
    fun `the old phone's answer is a signed POST the box can verify`() = runBlocking {
        val server = MockWebServer()
        server.start()
        try {
            val key = ByteArray(32) { 4 }
            val signer = OriginSigner("en_0badc0de", OriginSigner.encodeKey(key))
            val client = IngestClient(server.url("").toString().removeSuffix("/"), nowSec = { 1_793_000_000L }, keyPinned = { true })
            server.enqueue(MockResponse.Builder().code(200).body("""{"state":"active"}""").build())
            val body = WellnessStatusParser.replaceApproveBody(WellnessFixtures.OPERATOR, true)
            val reply = client.action(IngestClient.REPLACE_APPROVE, body, signer)
            assertEquals(WellnessRules.ReplaceOutcome.MOVED,
                WellnessRules.replaceOutcome(reply.status, WellnessOutbox.parseError(reply.body), true))
            val req = server.takeRequest(5, TimeUnit.SECONDS)!!
            assertEquals("/wellness/replace-approve", req.url.encodedPath)
            val sent = req.body!!.toByteArray()
            val f = req.headers[OriginSigner.HEADER]!!.removePrefix("v1;").split(';').associate { it.substringBefore('=') to it.substringAfter('=') }
            assertEquals(OriginSigner.signature(key, "en_0badc0de", 1_793_000_000L, f.getValue("nonce"), "POST",
                "/wellness/replace-approve", sent), f["sig"])
        } finally {
            server.close()
        }
    }

    // ── the new phone ──────────────────────────────────────────────────────────────────────

    @Test
    fun `an enrollment that waits for the old phone says so and is no live source yet`() {
        val pending = WellnessEnrollment.interpret(WellnessHttp(200,
            """{"enrollment_id":"en_00000002","epoch":2,"state":"replace_pending","sync_interval_min":60}"""))
        pending as WellnessEnrollment.Outcome.Enrolled
        assertTrue(pending.awaitsApproval)
        assertEquals(WellnessRules.AWAITING_APPROVAL_MESSAGE, pending.message)
        val active = WellnessEnrollment.interpret(WellnessHttp(200, """{"enrollment_id":"en_00000002","epoch":2,"state":"active"}"""))
        active as WellnessEnrollment.Outcome.Enrolled
        assertFalse(active.awaitsApproval)
        assertEquals(WellnessRules.ENROLLED_MESSAGE, active.message)

        val waiting = WellnessRules.EnrollmentState.fromWire("replace_pending")
        assertEquals(WellnessRules.EnrollmentState.REPLACE_PENDING, waiting)
        assertFalse("the chat does not sign for it", waiting in WellnessChatRules.SIGNING_STATES)
        assertFalse("no history import yet", WellnessRules.shouldStartBackfill(false, false, false, waiting))
        assertEquals(WellnessRules.StatusChip.NEEDS_ATTENTION, WellnessRules.statusChip(waiting, true, emptyList()))
    }

    @Test
    fun `the waiting phone keeps its own state until its signed status is the full one`() {
        val waiting = WellnessRules.EnrollmentState.REPLACE_PENDING
        val active = WellnessRules.EnrollmentState.ACTIVE
        // the four uncredentialed fields carry the OLD enrollment's state
        assertEquals(waiting, WellnessRules.shownState(active, boxCredentialed = false, local = waiting))
        assertEquals(waiting, WellnessRules.shownState(null, boxCredentialed = false, local = waiting))
        // approved: this phone's own signed status is credentialed
        assertEquals(active, WellnessRules.shownState(active, boxCredentialed = true, local = waiting))
        // everyone else as before: the box's view, else the phone's
        assertEquals(active, WellnessRules.shownState(active, boxCredentialed = false, local = WellnessRules.EnrollmentState.PAUSED))
        assertEquals(WellnessRules.EnrollmentState.PAUSED, WellnessRules.shownState(null, false, WellnessRules.EnrollmentState.PAUSED))
    }

    @Test
    fun `the waiting phone's trouble card says what to do on the other phone`() {
        val facts = WellnessRules.LocalFacts(sdk = HcSdkStatus.AVAILABLE, consentedCategories = emptySet(), granted = emptySet(),
            backgroundGranted = true, replacePending = true)
        assertEquals(WellnessRules.REPLACE_PENDING_TROUBLE, WellnessRules.localTroubles(facts).first())
        val card = WellnessRules.troubleCard(WellnessRules.REPLACE_PENDING_TROUBLE)!!
        assertEquals("Waiting for your other phone", card.title)
        assertEquals(WellnessRules.TroubleFix.NONE, card.fix)
        assertFalse(WellnessRules.localTroubles(facts.copy(replacePending = false)).contains(WellnessRules.REPLACE_PENDING_TROUBLE))
    }

    // ── the new phone's setup choices wait for the approval ───────────────────────────────

    private val setup = WellnessRules.Settings(
        localOnly = WellnessCatalog.DEFAULT_LOCAL_ONLY + (WellnessCategory.SLEEP to true),
        providersAllowed = setOf("anthropic"),
        googlePaidKeyConfirmed = false,
    )

    private fun credentialedStatus(state: String = "active", providers: String = "[\"openai\",\"google\"]") =
        WellnessStatusParser.parse("""{"state":"$state","enrolled":true,"troubles":[],"categories":{"sleep":"ask"},
            "local_only":{"sleep":false,"heart":true,"activity":true,"blood_pressure":true},
            "providers_allowed":$providers,"google_paid_key_confirmed":true}""")!!

    @Test
    fun `the setup choices are sent once this phone's own signed status is credentialed, and state the setup fields`() {
        val pending = WellnessRules.pendingSettingsOf(setup)
        assertEquals(listOf("anthropic"), pending.providersAllowed)
        assertEquals(true, pending.localOnly["sleep"])
        val body = Json.parseToJsonElement(WellnessRules.pendingSettingsBody(WellnessFixtures.OPERATOR, pending,
            credentialedStatus())!!).jsonObject
        assertEquals(setOf("operator", "local_only", "providers_allowed", "google_paid_key_confirmed"), body.keys)
        assertEquals(WellnessFixtures.OPERATOR, body.getValue("operator").jsonPrimitive.content)
        assertEquals(listOf("anthropic"), Json.parseToJsonElement(body.getValue("providers_allowed").toString())
            .let { (it as kotlinx.serialization.json.JsonArray).map { e -> e.jsonPrimitive.content } })
        // The setup's choices win over the old enrollment's, both ways.
        assertTrue(body.getValue("local_only").jsonObject.getValue("sleep").jsonPrimitive.boolean)
        assertFalse(body.getValue("local_only").jsonObject.getValue("heart").jsonPrimitive.boolean)
        assertFalse(body.getValue("google_paid_key_confirmed").jsonPrimitive.boolean)
        // "Only AI on my network" (no tick) is sent as an empty list, a narrowing like any other.
        val onlyLocal = WellnessRules.pendingSettingsOf(WellnessRules.Settings())
        val narrowed = Json.parseToJsonElement(WellnessRules.pendingSettingsBody(WellnessFixtures.OPERATOR, onlyLocal,
            credentialedStatus())!!).jsonObject
        assertEquals("[]", narrowed.getValue("providers_allowed").toString())
    }

    @Test
    fun `nothing is sent while the replacement still waits or when nothing waits`() {
        val pending = WellnessRules.pendingSettingsOf(setup)
        val unsigned = WellnessStatusParser.parse("""{"state":"active","enrolled":true,"source_label":"Old phone","last_sync":null}""")
        assertEquals(null, WellnessRules.pendingSettingsBody(WellnessFixtures.OPERATOR, pending, unsigned))
        assertEquals(null, WellnessRules.pendingSettingsBody(WellnessFixtures.OPERATOR, pending, null))
        assertEquals(null, WellnessRules.pendingSettingsBody(WellnessFixtures.OPERATOR, null, credentialedStatus()))
        assertEquals(null, WellnessRules.pendingSettingsBody("", pending, credentialedStatus()))
        assertEquals(null, WellnessRules.pendingSettingsBody(WellnessFixtures.OPERATOR, pending,
            credentialedStatus(state = "replace_pending")))
    }

    @Test
    fun `the box's answer to the waiting choices clears, keeps or reports them`() {
        assertEquals(WellnessRules.PendingSettingsOutcome.SAVED, WellnessRules.pendingSettingsOutcome(200))
        for (code in listOf(400, 403, 404, 409, 410, 422)) {
            assertEquals("$code", WellnessRules.PendingSettingsOutcome.REFUSED, WellnessRules.pendingSettingsOutcome(code))
        }
        for (code in listOf(-1, 401, 408, 429, 500, 503)) {
            assertEquals("$code", WellnessRules.PendingSettingsOutcome.RETRY, WellnessRules.pendingSettingsOutcome(code))
        }
    }

    @Test
    fun `the waiting choices survive the state file and an older file without them still loads`() {
        val store = WellnessSyncStateStore(java.io.File(WellnessFixtures.tempDir("pending"), "sync_state.json"))
        store.update { it.copy(state = "replace_pending", pendingSettings = WellnessRules.pendingSettingsOf(setup)) }
        assertEquals(WellnessRules.pendingSettingsOf(setup), store.load().pendingSettings)
        val old = WellnessSyncStateStore(java.io.File(WellnessFixtures.tempDir("pending-old"), "sync_state.json").also {
            it.parentFile!!.mkdirs(); it.writeText("""{"state":"active","interval_min":30}""")
        })
        assertEquals(null, old.load().pendingSettings)
        assertEquals(30, old.load().intervalMin)
    }
}
