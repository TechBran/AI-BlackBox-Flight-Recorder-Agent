package com.aiblackbox.portal.data.wellness

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * `AndroidBridge.wellnessSign(method, pathWithQuery, bodySha256Hex)` as PortalActivity calls it
 * (WellnessChatSigning.bridgeHeader over this phone's facts; the contract with the Portal unit,
 * spec §6.3): a header for an allow-listed request of the enrolled operator, null for anything
 * else, and never the key.
 */
class WellnessBridgeSignTest {

    private val op = WellnessFixtures.OPERATOR
    private val key = ByteArray(32) { (it + 40).toByte() }
    private val k = OriginSigner.encodeKey(key)

    private fun install(state: WellnessRules.EnrollmentState = WellnessRules.EnrollmentState.ACTIVE) =
        WellnessChatSigning.installForTest {
            WellnessSigningInterceptor.Facts(WellnessBindingRecord(op, "en_0f1c0f1c", 1, k), WellnessSyncState(state = state.wire),
                "http://box.invalid:9091")
        }

    @After fun tearDown() = WellnessChatSigning.installForTest(null)

    private val body = """{"messages":[{"role":"user","content":"Steps so far today?"}],"operator":"wellness-fixture"}"""
        .toByteArray(Charsets.UTF_8)
    private val sha = OriginSigner.sha256Hex(body)

    @Test
    fun `the Portal's chat turn on the enrolled phone gets a header that verifies over its body`() {
        install()
        val header = WellnessChatSigning.bridgeHeader(op, "POST", "/chat/stream", sha)
        assertNotNull(header)
        val f = header!!.removePrefix("v1;").split(';').associate { it.substringBefore('=') to it.substringAfter('=') }
        assertEquals(setOf("enr", "ts", "nonce", "sig"), f.keys)
        assertEquals(OriginSigner.signature(key, "en_0f1c0f1c", f.getValue("ts").toLong(), f.getValue("nonce"), "POST",
            "/chat/stream", body), f["sig"])
        assertFalse("the key never enters JavaScript", header.contains(k))
        val again = WellnessChatSigning.bridgeHeader(op, "POST", "/chat/stream", sha)!!
        assertFalse("a fresh nonce each time", again.substringAfter("nonce=").substringBefore(';') == f["nonce"])
    }

    @Test
    fun `a rejected path, another operator or no live enrollment get null`() {
        install()
        assertNull(WellnessChatSigning.bridgeHeader(op, "POST", "/wellness/erase", sha))
        assertNull(WellnessChatSigning.bridgeHeader(op, "PUT", "/wellness/settings", sha))
        assertNull(WellnessChatSigning.bridgeHeader(op, "POST", "/wellness/replace-approve", sha))
        assertNull(WellnessChatSigning.bridgeHeader("someone-else", "POST", "/chat/stream", sha))
        assertNotNull(WellnessChatSigning.bridgeHeader(op, "GET", "/wellness/ref/w-20261028-ab12", OriginSigner.sha256Hex(ByteArray(0))))
        install(WellnessRules.EnrollmentState.REVOKED)
        assertNull(WellnessChatSigning.bridgeHeader(op, "POST", "/chat/stream", sha))
        WellnessChatSigning.installForTest(null)
        assertNull("Health not wired in", WellnessChatSigning.bridgeHeader(op, "POST", "/chat/stream", sha))
        WellnessChatSigning.installForTest { error("unreadable binding") }
        assertNull("an unreadable binding signs nothing", WellnessChatSigning.bridgeHeader(op, "POST", "/chat/stream", sha))
    }
}
