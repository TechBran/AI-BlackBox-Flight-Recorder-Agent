package com.aiblackbox.portal.data.remote

import com.aiblackbox.portal.data.wellness.BindDecision
import com.aiblackbox.portal.data.wellness.OriginSigner
import com.aiblackbox.portal.data.wellness.WELLNESS_BIND
import com.aiblackbox.portal.data.wellness.WELLNESS_SYNC
import com.aiblackbox.portal.data.wellness.WELLNESS_UNBIND
import com.aiblackbox.portal.data.wellness.WellnessBindRequest
import com.aiblackbox.portal.data.wellness.WellnessBinding
import com.aiblackbox.portal.data.wellness.WellnessBindingRecord
import com.aiblackbox.portal.data.wellness.WellnessFiles
import com.aiblackbox.portal.data.wellness.WellnessFixtures
import com.aiblackbox.portal.data.wellness.WellnessPort
import com.aiblackbox.portal.data.wellness.routeWellness
import com.aiblackbox.portal.data.wellness.wellnessOperatorFor
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The listener's scope check for the M7 box -> phone health routes: `/wellness/bind` is
 * accepted only for the operator the user chose AND with the live 2-minute intent nonce;
 * `/wellness/sync` and `/wellness/unbind` only for the operator this phone is the health
 * source for, which is NOT the chat-bound operator (rule E4). Fail-closed everywhere.
 */
class RemoteControlAuthorizeTest {

    private val op = WellnessFixtures.OPERATOR
    private val tailnet = "100.88.0.7"
    private var now = 1_000_000L
    private val files = WellnessFiles(WellnessFixtures.tempDir("authorize"))
    private val binding = WellnessBinding(files, clock = { now })
    private val key = OriginSigner.encodeKey(ByteArray(32) { 3 })
    private var synced = 0
    private var bound: WellnessBindingRecord? = null

    private val port = object : WellnessPort {
        override fun bindOperator() = binding.bindOperator()
        override fun enrolledOperator() = binding.enrolledOperator()
        override fun acceptBind(request: WellnessBindRequest) = binding.acceptBind(request)
        override fun current() = binding.current()
        override fun requestSync() { synced++ }
        override fun clearAll() = binding.clearAll()
        override fun onBound(record: WellnessBindingRecord) { bound = record }
    }

    private fun bindBody(nonce: String, operator: String = op, enr: String = "en_0badc0de", epoch: Int = 1) =
        """{"operator":"$operator","enrollment_id":"$enr","epoch":$epoch,"nonce":"$nonce","k":"$key"}"""

    /** authorize() as the server calls it for a wellness path. */
    private fun auth(method: String, path: String, requestOperator: String, boundChatOperator: String = "someone-else") =
        authorize(method, path, tailnet, requestOperator, boundChatOperator,
            wellnessOperator = wellnessOperatorFor(path, port))

    // ── authorize() ────────────────────────────────────────────────────────────────────────

    @Test
    fun `bind and sync are refused when no intent and no enrollment exist`() {
        for (path in listOf(WELLNESS_BIND, WELLNESS_SYNC)) {
            assertEquals(path, 403, auth("POST", path, op)?.status)
        }
    }

    @Test
    fun `an unbind on a phone with no enrollment reaches the router's idempotent 200`() {
        // The box's best-effort unbind after an erase, to a phone that already cleared itself.
        assertNull(auth("POST", WELLNESS_UNBIND, op))
        val r = routeWellness("POST", WELLNESS_UNBIND, """{"operator":"$op","enrollment_id":"en_0badc0de"}""", port)
        assertEquals(200, r.status)
        assertTrue(r.json.contains("not_set_up"))
        // Still fail-closed for a blank operator and for any other method.
        assertEquals(403, auth("POST", WELLNESS_UNBIND, "")?.status)
        assertEquals(403, auth("GET", WELLNESS_UNBIND, op)?.status)
    }

    @Test
    fun `sync and unbind naming another operator than the enrolled one are refused by the router too`() {
        // authorize() saw no enrollment, then a bind landed before the router ran.
        val intent = binding.beginIntent(op)
        routeWellness("POST", WELLNESS_BIND, bindBody(intent.nonce), port)
        val body = """{"operator":"someone-else","enrollment_id":"en_0badc0de"}"""
        assertEquals(403, routeWellness("POST", WELLNESS_UNBIND, body, port).status)
        assertEquals(403, routeWellness("POST", WELLNESS_SYNC, body, port).status)
        assertNotNull(binding.current())
        assertEquals(0, synced)
    }

    @Test
    fun `bind is scoped to the operator the user chose, not the chat operator`() {
        binding.beginIntent(op)
        assertNull(auth("POST", WELLNESS_BIND, op, boundChatOperator = "someone-else"))
        assertEquals(403, auth("POST", WELLNESS_BIND, "someone-else", boundChatOperator = "someone-else")?.status)
        assertEquals(403, auth("POST", WELLNESS_BIND, "")?.status)
    }

    @Test
    fun `an expired intent closes bind again`() {
        binding.beginIntent(op)
        now += WellnessBinding.INTENT_LIFE_MS
        assertEquals(403, auth("POST", WELLNESS_BIND, op)?.status)
    }

    @Test
    fun `sync and unbind are scoped to the enrolled operator`() {
        val intent = binding.beginIntent(op)
        assertTrue(binding.acceptBind(WellnessBindRequest(op, "en_0badc0de", 1, intent.nonce, key)) is BindDecision.Accepted)
        assertNull(auth("POST", WELLNESS_SYNC, op, boundChatOperator = "someone-else"))
        assertNull(auth("POST", WELLNESS_UNBIND, op))
        assertEquals(403, auth("POST", WELLNESS_SYNC, "someone-else", boundChatOperator = "someone-else")?.status)
        // After enrollment the intent is spent: a second bind is refused at authorize().
        assertEquals(403, auth("POST", WELLNESS_BIND, op)?.status)
    }

    @Test
    fun `off-tailnet sources are refused before the wellness check`() {
        binding.beginIntent(op)
        val r = authorize("POST", WELLNESS_BIND, "192.168.1.9", op, op, wellnessOperator = op)
        assertEquals(403, r?.status)
        assertTrue(r!!.json.contains("tailnet"))
    }

    @Test
    fun `existing routes are unaffected by the wellness clause`() {
        assertNull(authorize("POST", "/task", tailnet, op, op, wellnessOperator = ""))
        assertNull(authorize("GET", "/healthz", tailnet, "", op))
    }

    // ── routeWellness(): the bind needs the LIVE nonce ─────────────────────────────────────

    @Test
    fun `bind with the live nonce stores the key and schedules the first sync`() {
        val intent = binding.beginIntent(op)
        val r = routeWellness("POST", WELLNESS_BIND, bindBody(intent.nonce), port)
        assertEquals(200, r.status)
        assertEquals("en_0badc0de", binding.current()!!.enrollmentId)
        assertEquals(key, binding.current()!!.k)
        assertNotNull(bound)
        assertNull(binding.liveIntent())
    }

    @Test
    fun `bind with a wrong or stale nonce is 403 and stores nothing`() {
        binding.beginIntent(op)
        assertEquals(403, routeWellness("POST", WELLNESS_BIND, bindBody("f".repeat(32)), port).status)
        assertNull(binding.current())
        val intent = binding.beginIntent(op)
        now += WellnessBinding.INTENT_LIFE_MS + 1
        assertEquals(403, routeWellness("POST", WELLNESS_BIND, bindBody(intent.nonce), port).status)
        assertNull(binding.current())
    }

    @Test
    fun `bind naming another operator is 403 even with the right nonce`() {
        val intent = binding.beginIntent(op)
        val r = routeWellness("POST", WELLNESS_BIND, bindBody(intent.nonce, operator = "someone-else"), port)
        assertEquals(403, r.status)
        assertTrue(r.json.contains("operator_mismatch"))
        assertNull(binding.current())
    }

    @Test
    fun `malformed bind payloads are 400`() {
        val intent = binding.beginIntent(op)
        assertEquals(400, routeWellness("POST", WELLNESS_BIND, "{not json", port).status)
        assertEquals(400, routeWellness("POST", WELLNESS_BIND, bindBody(intent.nonce, enr = "en_XYZ"), port).status)
        assertEquals(400, routeWellness("POST", WELLNESS_BIND, bindBody(intent.nonce, epoch = 0), port).status)
        val shortKey = bindBody(intent.nonce).replace(key, OriginSigner.encodeKey(ByteArray(8)))
        assertEquals(400, routeWellness("POST", WELLNESS_BIND, shortKey, port).status)
        assertNull(binding.current())
    }

    @Test
    fun `sync nudges with 202, unbind clears everything, stale enrollment ids are refused`() {
        val intent = binding.beginIntent(op)
        routeWellness("POST", WELLNESS_BIND, bindBody(intent.nonce), port)
        assertEquals(409, routeWellness("POST", WELLNESS_SYNC, """{"operator":"$op","enrollment_id":"en_11111111"}""", port).status)
        assertEquals(202, routeWellness("POST", WELLNESS_SYNC, """{"operator":"$op","enrollment_id":"en_0badc0de"}""", port).status)
        assertEquals(1, synced)
        assertEquals(409, routeWellness("POST", WELLNESS_UNBIND, """{"operator":"$op"}""", port).status)
        assertNotNull(binding.current())
        assertEquals(200, routeWellness("POST", WELLNESS_UNBIND, """{"operator":"$op","enrollment_id":"en_0badc0de"}""", port).status)
        assertNull(binding.current())
        // Idempotent once nothing is bound.
        assertEquals(200, routeWellness("POST", WELLNESS_UNBIND, """{"operator":"$op","enrollment_id":"en_0badc0de"}""", port).status)
    }

    @Test
    fun `wrong method, unknown path and a missing port`() {
        assertEquals(405, routeWellness("GET", WELLNESS_BIND, "", port).status)
        assertEquals(404, routeWellness("POST", "/wellness/other", "{}", port).status)
        assertEquals(503, routeWellness("POST", WELLNESS_BIND, "{}", null).status)
        assertEquals("", wellnessOperatorFor(WELLNESS_SYNC, null))
    }
}
