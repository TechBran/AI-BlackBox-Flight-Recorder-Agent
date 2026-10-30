package com.aiblackbox.portal.data.wellness

import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The phone's half of `POST /wellness/enroll`: the request body is the contract's, the box's
 * bind (simulated here the way the box does it, during the enroll call) must carry the live
 * nonce, and every refusal maps to plain words.
 */
class WellnessEnrollmentTest {

    private var now = 1_000_000L
    private val binding = WellnessBinding(WellnessFiles(WellnessFixtures.tempDir("enroll")), clock = { now })
    private val key = OriginSigner.encodeKey(ByteArray(32) { 5 })
    private val report = """{"phone":{"model":"SM-TEST0"},"origins":[]}"""

    /** A stand-in box: reads the nonce from the enroll body and binds back with it. */
    private fun box(status: Int = 200, bindWith: ((String) -> String)? = { it }, body: String? = null): suspend (String) -> WellnessHttp = { req ->
        val nonce = Json.parseToJsonElement(req).jsonObject.getValue("nonce").jsonPrimitive.content
        if (status == 200 && bindWith != null) {
            binding.acceptBind(WellnessBindRequest(WellnessFixtures.OPERATOR, "en_12ab34cd", 2, bindWith(nonce), key))
        }
        WellnessHttp(status, body ?: """{"enrollment_id":"en_12ab34cd","epoch":2,"state":"active","op_key_hint":null,
            "sync_interval_min":60,"policy_version":"2026-10-29"}""")
    }

    private fun enrollment(post: suspend (String) -> WellnessHttp, ips: List<String> = listOf("100.101.102.103")) =
        WellnessEnrollment(binding, post, { ips }, bindWaitMs = 200)

    @Test
    fun `the request carries the contract's fields`() {
        val body = WellnessEnrollment.requestBody(WellnessFixtures.OPERATOR, "a".repeat(32), listOf("100.64.0.9"),
            "b".repeat(32), report, WellnessCatalog.DEFAULT_CONSENT, replace = false, assignToMe = true)
        val obj = Json.parseToJsonElement(body).jsonObject
        assertEquals(listOf("operator", "install_id", "tailnet_ips", "nonce", "capability_report", "categories", "replace",
            "assign_to_me"), obj.keys.toList())
        assertEquals(listOf("100.64.0.9"), obj.getValue("tailnet_ips").jsonArray.map { it.jsonPrimitive.content })
        assertEquals(mapOf("sleep" to "ask", "heart" to "ask", "activity" to "ask", "blood_pressure" to "off"),
            obj.getValue("categories").jsonObject.mapValues { it.value.jsonPrimitive.content })
        assertTrue(obj.getValue("assign_to_me").jsonPrimitive.boolean)
        assertEquals("SM-TEST0", obj.getValue("capability_report").jsonObject.getValue("phone").jsonObject
            .getValue("model").jsonPrimitive.content)
    }

    @Test
    fun `a 200 with the bind received is Enrolled`() = runBlocking {
        val outcome = enrollment(box()).enroll(WellnessFixtures.OPERATOR, WellnessCatalog.DEFAULT_CONSENT, report, true, false)
        outcome as WellnessEnrollment.Outcome.Enrolled
        assertEquals("en_12ab34cd", outcome.enrollmentId)
        assertEquals(2, outcome.epoch)
        assertEquals(60, outcome.syncIntervalMin)
        assertEquals("en_12ab34cd", binding.current()!!.enrollmentId)
    }

    @Test
    fun `a 200 whose bind never arrived (or used another nonce) is BindMissing`() = runBlocking {
        val outcome = enrollment(box(bindWith = { "0".repeat(32) }))
            .enroll(WellnessFixtures.OPERATOR, WellnessCatalog.DEFAULT_CONSENT, report, true, false)
        assertTrue(outcome is WellnessEnrollment.Outcome.BindMissing)
        assertNull(binding.current())
    }

    @Test
    fun `refusals map to their words and spend the intent`() = runBlocking {
        val cases = listOf(
            409 to """{"error":"owner_mismatch"}""" to WellnessEnrollment.Outcome.OwnerMismatch,
            409 to """{"error":"already_enrolled"}""" to WellnessEnrollment.Outcome.AlreadyEnrolled,
            409 to """{"error":"device_in_use"}""" to WellnessEnrollment.Outcome.DeviceInUse,
            424 to """{"error":"not_paired","fix":"Open the BlackBox app once on this phone"}""" to WellnessEnrollment.Outcome.NotPaired,
            502 to """{"error":"bind_failed"}""" to WellnessEnrollment.Outcome.BindFailed,
            403 to """{"error":"not_allowed"}""" to WellnessEnrollment.Outcome.NotAllowed,
        )
        for ((reply, expected) in cases) {
            val (status, body) = reply
            val outcome = enrollment(box(status, null, body)).enroll(WellnessFixtures.OPERATOR,
                WellnessCatalog.DEFAULT_CONSENT, report, false, false)
            assertEquals(expected, outcome)
            assertNull(binding.liveIntent())
        }
        val unreachable = enrollment({ WellnessHttp(-1, "") }).enroll(WellnessFixtures.OPERATOR,
            WellnessCatalog.DEFAULT_CONSENT, report, false, false)
        assertEquals(WellnessEnrollment.Outcome.Unreachable, unreachable)
    }

    @Test
    fun `a phone off the tailnet does not even ask`() = runBlocking {
        var called = false
        val outcome = enrollment({ called = true; WellnessHttp(200, "{}") }, ips = emptyList())
            .enroll(WellnessFixtures.OPERATOR, WellnessCatalog.DEFAULT_CONSENT, report, false, false)
        assertEquals(WellnessEnrollment.Outcome.NotOnTailnet, outcome)
        assertTrue(!called)
    }
}
