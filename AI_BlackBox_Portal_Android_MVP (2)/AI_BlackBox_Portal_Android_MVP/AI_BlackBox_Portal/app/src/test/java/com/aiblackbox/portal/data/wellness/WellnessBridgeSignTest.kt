package com.aiblackbox.portal.data.wellness

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

/**
 * The in-app Portal's request signer (spec §6.3), the contract with the Portal unit: a
 * `BlackBoxWellness` message channel only the paired box's main frame can use (never an
 * AndroidBridge method, which every frame gets, the sandboxed app-preview iframe among them).
 * A header for an allow-listed request of the enrolled operator, null for anything else, and
 * never the key.
 */
class WellnessBridgeSignTest {

    private val op = WellnessFixtures.OPERATOR
    private val key = ByteArray(32) { (it + 40).toByte() }
    private val k = OriginSigner.encodeKey(key)
    private val paired = "http://100.64.0.10:9091"

    private fun install(
        state: WellnessRules.EnrollmentState = WellnessRules.EnrollmentState.ACTIVE,
        boundTo: String? = "http://box.invalid:9091",
        adopt: (String) -> Boolean = { false },
    ) = WellnessChatSigning.installForTest {
        WellnessSigningInterceptor.Facts(WellnessBindingRecord(op, "en_0f1c0f1c", 1, k, boxOrigin = boundTo),
            WellnessSyncState(state = state.wire), "http://box.invalid:9091", adoptOrigin = adopt)
    }

    @After fun tearDown() = WellnessChatSigning.installForTest(null)

    private val body = """{"messages":[{"role":"user","content":"Steps so far today?"}],"operator":"wellness-fixture"}"""
        .toByteArray(Charsets.UTF_8)
    private val sha = OriginSigner.sha256Hex(body)
    private val emptySha = OriginSigner.sha256Hex(ByteArray(0))

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
        assertNotNull(WellnessChatSigning.bridgeHeader(op, "GET", "/wellness/ref/w-20261028-ab12?operator=$op", emptySha))
        install(WellnessRules.EnrollmentState.REVOKED)
        assertNull(WellnessChatSigning.bridgeHeader(op, "POST", "/chat/stream", sha))
        WellnessChatSigning.installForTest(null)
        assertNull("Health not wired in", WellnessChatSigning.bridgeHeader(op, "POST", "/chat/stream", sha))
        WellnessChatSigning.installForTest { error("unreadable binding") }
        assertNull("an unreadable binding signs nothing", WellnessChatSigning.bridgeHeader(op, "POST", "/chat/stream", sha))
    }

    @Test
    fun `the operator is the Portal's own, never one the request names`() {
        install()
        // A POST is for the Portal's operator: a query naming the enrolled one changes nothing.
        assertNull(WellnessChatSigning.bridgeHeader("someone-else", "POST", "/chat/stream?operator=$op", sha))
        assertNotNull(WellnessChatSigning.bridgeHeader(op, "POST", "/chat/stream?operator=someone-else", sha))
        // A GET names exactly one operator, the Portal's.
        assertNotNull(WellnessChatSigning.bridgeHeader(op, "GET", "/wellness/status?operator=$op", emptySha))
        assertNull(WellnessChatSigning.bridgeHeader("someone-else", "GET", "/wellness/status?operator=$op", emptySha))
        assertNull(WellnessChatSigning.bridgeHeader(op, "GET", "/wellness/status", emptySha))
        assertNull(WellnessChatSigning.bridgeHeader(op, "GET", "/wellness/status?operator=$op&operator=someone-else", emptySha))
        assertNull(WellnessChatSigning.bridgeHeader(op, "GET", "/wellness/status?operator=someone-else&operator=$op", emptySha))
        // No Portal operator: nothing.
        assertNull(WellnessChatSigning.bridgeHeader(null, "POST", "/chat/stream", sha))
        assertNull(WellnessChatSigning.bridgeHeader("  ", "POST", "/chat/stream", sha))
        assertEquals(op, WellnessChatRules.bridgeOperator("post", "/chat?operator=x", op))
        assertNull(WellnessChatRules.bridgeOperator("GET", "/wellness/disclosures?operator=x", op))
    }

    @Test
    fun `the in-app Portal signs its ledger read like any other read, naming its operator once`() {
        install()
        assertNotNull(WellnessChatSigning.bridgeHeader(op, "GET", "/fossil/snapshot/SNAP-20261028-42?operator=$op", emptySha))
        assertNull(WellnessChatSigning.bridgeHeader(op, "GET", "/fossil/snapshot/SNAP-20261028-42", emptySha))
        assertNull(WellnessChatSigning.bridgeHeader("someone-else", "GET", "/fossil/snapshot/SNAP-42?operator=$op", emptySha))
        assertNull(WellnessChatSigning.bridgeHeader(op, "POST", "/fossil/snapshot/SNAP-42?operator=$op", emptySha))
    }

    // ── the key's box (contract (f)) ───────────────────────────────────────────────────────

    @Test
    fun `an app re-pointed at another box gives its Portal no proof`() {
        install(boundTo = "http://100.64.0.99:9091")
        assertNull(WellnessChatSigning.bridgeHeader(op, "POST", "/chat/stream", sha))
        assertNull(WellnessChatSigning.bridgeHeader(op, "GET", "/wellness/ref/w-20261028-ab12?operator=$op", emptySha))
    }

    @Test
    fun `a binding older than box_origin signs for the Portal once it has adopted the paired box`() {
        val adopted = mutableListOf<String>()
        install(boundTo = null, adopt = { adopted += it; true })
        assertNotNull(WellnessChatSigning.bridgeHeader(op, "POST", "/chat/stream", sha))
        assertEquals(listOf("http://box.invalid:9091"), adopted)
        install(boundTo = null)
        assertNull("not adopted, not signed", WellnessChatSigning.bridgeHeader(op, "POST", "/chat/stream", sha))
    }

    // ── who may ask ────────────────────────────────────────────────────────────────────────

    @Test
    fun `only the paired box's main frame gets a proof`() {
        assertTrue(WellnessChatRules.bridgeCallerAllowed(paired, true, paired, paired))
        assertTrue("a trailing slash is the same origin", WellnessChatRules.bridgeCallerAllowed("$paired/", true, paired, paired))
        assertFalse("a same-origin iframe", WellnessChatRules.bridgeCallerAllowed(paired, false, paired, paired))
        assertFalse("the sandboxed app preview (opaque origin)", WellnessChatRules.bridgeCallerAllowed("null", true, paired, paired))
        assertFalse("another origin", WellnessChatRules.bridgeCallerAllowed("http://100.64.0.10:8061", true, paired, paired))
        assertFalse("the page left the box", WellnessChatRules.bridgeCallerAllowed(paired, true, paired, "https://example.com"))
        assertFalse("no page origin yet", WellnessChatRules.bridgeCallerAllowed(paired, true, paired, null))
        assertFalse("unpaired", WellnessChatRules.bridgeCallerAllowed(paired, true, null, paired))
        assertFalse(WellnessChatRules.bridgeCallerAllowed(null, true, paired, paired))
    }

    // ── the message contract ───────────────────────────────────────────────────────────────

    @Test
    fun `a sign request parses, and its answer echoes the id with the header or null`() {
        val r = WellnessChatRules.parseBridgeRequest("""{"id":"p-7","method":"POST","path":"/chat/stream","sha256":"$sha"}""")!!
        assertEquals(JsonPrimitive("p-7"), r.id)
        assertEquals("POST", r.method)
        assertEquals("/chat/stream", r.pathWithQuery)
        assertEquals(sha, r.bodySha256Hex)
        val numeric = WellnessChatRules.parseBridgeRequest("""{"id":12,"method":"GET","path":"/wellness/status?operator=$op","sha256":"$emptySha"}""")!!
        assertEquals("12", numeric.id.content)
        assertFalse(numeric.id.isString)

        val yes = Json.parseToJsonElement(WellnessChatRules.bridgeReply(r.id, "v1;enr=en_0f1c0f1c;ts=1;nonce=a;sig=b")).jsonObject
        assertEquals(JsonPrimitive("p-7"), yes["id"])
        assertEquals("v1;enr=en_0f1c0f1c;ts=1;nonce=a;sig=b", (yes["value"] as JsonPrimitive).content)
        val no = Json.parseToJsonElement(WellnessChatRules.bridgeReply(numeric.id, null)).jsonObject
        assertEquals(JsonPrimitive(12), no["id"])
        assertEquals(JsonNull, no["value"])
    }

    @Test
    fun `a malformed request gets no answer at all`() {
        for (bad in listOf(null, "", "not json", "[]", """{"method":"POST","path":"/chat","sha256":"$sha"}""",
                """{"id":"x","path":"/chat","sha256":"$sha"}""", """{"id":"x","method":"POST","sha256":"$sha"}""",
                """{"id":"x","method":"POST","path":"/chat"}""", """{"id":{"a":1},"method":"POST","path":"/chat","sha256":"$sha"}""",
                """{"id":"has space","method":"POST","path":"/chat","sha256":"$sha"}""", """{"id":1.5,"method":"POST","path":"/chat","sha256":"$sha"}""",
                """{"id":"x","method":"POST","path":"/chat","sha256":"$sha","pad":"${"a".repeat(3000)}"}""")) {
            assertNull(bad, WellnessChatRules.parseBridgeRequest(bad))
        }
    }

    // ── the wiring ─────────────────────────────────────────────────────────────────────────

    private fun source(rel: String): String {
        var dir: File? = File(".").absoluteFile
        repeat(8) {
            val d = dir ?: return@repeat
            for (candidate in listOf(File(d, rel), File(d, "app/$rel"))) if (candidate.isFile) return candidate.readText()
            dir = d.parentFile
        }
        fail("could not locate $rel from ${File(".").absolutePath}")
        error("unreachable")
    }

    @Test
    fun `PortalActivity signs through the main-frame channel, and no frame-wide bridge method signs`() {
        val portal = source("src/main/java/com/aiblackbox/portal/PortalActivity.kt")
        assertFalse("AndroidBridge.wellnessSign is gone (every frame gets AndroidBridge)", "fun wellnessSign(" in portal)
        assertTrue("the channel is restricted to the paired origin",
            "WebViewCompat.addWebMessageListener(web, WellnessChatRules.BRIDGE_CHANNEL, setOf(paired))" in portal)
        val handler = portal.substringAfter("private fun onWellnessSignMessage(").substringBefore("\n    }\n")
        assertTrue("each message's sender is checked",
            "WellnessChatRules.bridgeCallerAllowed(sourceOrigin, isMainFrame, pairedOrigin, pageOrigin)" in handler)
        assertTrue("the operator is the Portal's own pick", "localStorage.getItem('bbx_operator')" in handler)
        assertFalse("nothing about it is logged", "Log." in handler)
        assertTrue("wellnessOpenSettings still opens with the gate",
            portal.substringAfter("fun wellnessOpenSettings()").substringBefore("}").contains("bridgeAllowed(\"wellnessOpenSettings\")"))
    }
}
