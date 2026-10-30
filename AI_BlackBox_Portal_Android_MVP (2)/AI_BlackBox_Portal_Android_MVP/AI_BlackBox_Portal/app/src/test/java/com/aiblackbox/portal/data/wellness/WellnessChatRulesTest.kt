package com.aiblackbox.portal.data.wellness

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.LocalDateTime
import java.time.ZoneId
import java.util.Locale

/**
 * The enrolled phone's chat rules (spec §6.1 "Chat", §6.3, §3.7): what may be signed, by the
 * interceptor and by the WebView bridge, what the footer says, and when Show numbers is
 * offered. Operator `wellness-fixture` and synthetic keys only.
 */
class WellnessChatRulesTest {

    private val op = WellnessFixtures.OPERATOR
    private val key = OriginSigner.encodeKey(ByteArray(32) { it.toByte() })
    private val binding = WellnessBindingRecord(operator = op, enrollmentId = "en_0f1c0f1c", epoch = 1, k = key)
    private fun state(s: WellnessRules.EnrollmentState = WellnessRules.EnrollmentState.ACTIVE) =
        WellnessSyncState(state = s.wire)

    private fun decide(
        b: WellnessBindingRecord? = binding,
        st: WellnessSyncState? = state(),
        operator: String? = op,
        method: String = "POST",
        path: String = WellnessChatRules.CHAT_STREAM,
    ) = WellnessChatRules.signingDecision(b, st, operator, method, path)

    private fun unsigned(reason: String) = WellnessChatRules.SigningDecision.Unsigned(reason)

    // ── the allow-list ─────────────────────────────────────────────────────────────────────

    @Test
    fun `only the allow-listed paths with their method are signable`() {
        val yes = listOf("POST" to "/chat/stream", "POST" to "/chat", "GET" to "/wellness/status",
            "GET" to "/wellness/disclosures", "GET" to "/wellness/ref/w-20261028-ab12", "post" to "/chat/stream",
            "GET" to "/fossil/snapshot/SNAP-20261028-42", "GET" to "/fossil/snapshot/SNAP-1", "get" to "/fossil/snapshot/SNAP-7")
        for ((m, p) in yes) assertTrue("$m $p", WellnessChatRules.isSignable(m, p))
        val no = listOf(
            "GET" to "/chat/stream", "PUT" to "/chat", "POST" to "/wellness/status", "POST" to "/chat/save",
            "POST" to "/wellness/erase", "POST" to "/wellness/settings", "PUT" to "/wellness/settings",
            "POST" to "/wellness/replace-approve", "GET" to "/wellness/ref/", "GET" to "/wellness/ref/../erase",
            "GET" to "/wellness/ref/w-20261028-ab12/x", "GET" to "/wellness/ref/W-20261028-AB12",
            "POST" to "/chat/stream/", "POST" to "/chat%2Fstream", "GET" to "/wellness/statusx",
            // the ledger read: GET only, one well-formed snapshot id, nothing after it
            "POST" to "/fossil/snapshot/SNAP-1", "DELETE" to "/fossil/snapshot/SNAP-1", "GET" to "/fossil/snapshot/",
            "GET" to "/fossil/snapshot/SNAP-", "GET" to "/fossil/snapshot/snap-1", "GET" to "/fossil/snapshot/SNAP-1/x",
            "GET" to "/fossil/snapshot/SNAP-2026102-1", "GET" to "/fossil/snapshot/../wellness/erase",
            "GET" to "/fossil/snapshot/SNAP-1%2F..", "GET" to "/fossil/snapshot/SNAP-1234567890123",
            "GET" to "/fossil/hybrid", "GET" to "/api/snapshots/recent",
        )
        for ((m, p) in no) assertFalse("$m $p", WellnessChatRules.isSignable(m, p))
    }

    @Test
    fun `the ledger read's path takes the box's two id forms and nothing else`() {
        assertEquals("/fossil/snapshot/SNAP-20261028-42", WellnessChatRules.ledgerPath("SNAP-20261028-42"))
        assertEquals("/fossil/snapshot/SNAP-42", WellnessChatRules.ledgerPath(" SNAP-42 "))
        assertNull(WellnessChatRules.ledgerPath("snap-42"))
        assertNull(WellnessChatRules.ledgerPath("SNAP-42?operator=x"))
        assertNull(WellnessChatRules.ledgerPath("../wellness/erase"))
        assertTrue(WellnessChatRules.isLedgerRead("/fossil/snapshot/SNAP-42"))
        assertFalse(WellnessChatRules.isLedgerRead("/wellness/ref/w-20261028-ab12"))
        // a refused proof (and only that) is asked again unsigned
        for (s in listOf(401, 409, 410)) assertTrue("$s", WellnessChatRules.proofRefused(s))
        for (s in listOf(200, 400, 403, 404, 500, -1)) assertFalse("$s", WellnessChatRules.proofRefused(s))
    }

    @Test
    fun `a ledger read is for the operator on screen, never for one a body or query names`() {
        val ledger = "/fossil/snapshot/SNAP-42"
        assertEquals(op, WellnessChatRules.requestOperator(ledger, null, null, op))
        assertEquals(op, WellnessChatRules.requestOperator(ledger, null, op, " $op "))
        assertNull("a query naming someone else", WellnessChatRules.requestOperator(ledger, null, "someone-else", op))
        assertNull("nobody on screen", WellnessChatRules.requestOperator(ledger, null, null, null))
        assertNull(WellnessChatRules.requestOperator(ledger, null, op, " "))
        // every other path: the body's operator, else the query's; the screen never overrides them
        val body = """{"operator":"wellness-fixture"}""".toByteArray()
        assertEquals(op, WellnessChatRules.requestOperator("/chat/stream", body, null, "someone-else"))
        assertNull(WellnessChatRules.requestOperator("/wellness/status", null, null, op))
        assertEquals("someone-else", WellnessChatRules.requestOperator("/wellness/status", null, "someone-else", op))
        // so another operator on screen leaves the enrolled phone's ledger read unsigned
        val other = WellnessChatRules.requestOperator(ledger, null, null, "someone-else")
        assertEquals(unsigned(WellnessChatRules.UNSIGNED_OTHER_OPERATOR), decide(operator = other, method = "GET", path = ledger))
        assertTrue(decide(operator = WellnessChatRules.requestOperator(ledger, null, null, op), method = "GET", path = ledger)
            is WellnessChatRules.SigningDecision.Sign)
    }

    // ── the key's box (contract (f)) ───────────────────────────────────────────────────────

    @Test
    fun `an origin is scheme, host and a port only when it is not the default`() {
        assertEquals("http://100.64.0.10:9091", WellnessChatRules.originOf("http://100.64.0.10:9091/chat/stream?x=1"))
        assertEquals("https://box.invalid", WellnessChatRules.originOf("https://box.invalid:443/"))
        assertEquals("http://box.invalid", WellnessChatRules.originOf("HTTP://Box.Invalid:80"))
        assertEquals("http://[fd7a:115c:a1e0::1]:9091", WellnessChatRules.originOf("http://[fd7a:115c:a1e0::1]:9091/"))
        assertNull(WellnessChatRules.originOf("not a url"))
        assertNull(WellnessChatRules.originOf(null))
    }

    @Test
    fun `the key signs only for the box that delivered it, while the app is paired with that box`() {
        val a = "http://100.64.0.10:9091"
        val b = "http://100.64.0.20:9091"
        val pinned = WellnessChatRules.KeyUse.Pinned
        assertEquals(pinned, WellnessChatRules.keyUse(a, "$a/chat/stream", a))
        assertEquals(pinned, WellnessChatRules.keyUse("$a/", "$a/fossil/snapshot/SNAP-1", "$a/"))
        // the app re-pointed at another box (the same operator name can exist there): no proof for it
        assertEquals(WellnessChatRules.KeyUse.Refused(WellnessChatRules.KEY_OTHER_BOX), WellnessChatRules.keyUse(a, "$b/chat/stream", b))
        // a request to a host other than the paired box, or no pairing at all
        assertEquals(WellnessChatRules.KeyUse.Refused(WellnessChatRules.KEY_OTHER_HOST), WellnessChatRules.keyUse(a, "$b/chat", a))
        assertEquals(WellnessChatRules.KeyUse.Refused(WellnessChatRules.KEY_OTHER_HOST),
            WellnessChatRules.keyUse(a, "http://100.64.0.10:9092/chat", a))
        assertEquals(WellnessChatRules.KeyUse.Refused(WellnessChatRules.KEY_UNPAIRED), WellnessChatRules.keyUse(a, "$a/chat", null))
        // a binding older than box_origin adopts the paired box of its first signed use
        assertEquals(WellnessChatRules.KeyUse.Adopt(a), WellnessChatRules.keyUse(null, "$a/chat/stream", "$a/"))
        assertEquals(WellnessChatRules.KeyUse.Adopt(a), WellnessChatRules.keyUse(" ", "$a/chat/stream", a))
        assertEquals(WellnessChatRules.KeyUse.Refused(WellnessChatRules.KEY_OTHER_HOST), WellnessChatRules.keyUse(null, "$b/chat", a))
    }

    // ── the decision matrix ────────────────────────────────────────────────────────────────

    @Test
    fun `the enrolled operator's chat turn is signed with its enrollment`() {
        val d = decide()
        assertTrue(d is WellnessChatRules.SigningDecision.Sign)
        d as WellnessChatRules.SigningDecision.Sign
        assertEquals("en_0f1c0f1c", d.enrollmentId)
        assertEquals(key, d.key)
    }

    @Test
    fun `no binding, another operator, no operator and a path off the list go out unsigned`() {
        assertEquals(unsigned(WellnessChatRules.UNSIGNED_NOT_ENROLLED), decide(b = null))
        assertEquals(unsigned(WellnessChatRules.UNSIGNED_OTHER_OPERATOR), decide(operator = "someone-else"))
        assertEquals(unsigned(WellnessChatRules.UNSIGNED_OTHER_OPERATOR), decide(operator = "Wellness-Fixture"))
        assertEquals(unsigned(WellnessChatRules.UNSIGNED_NO_OPERATOR), decide(operator = null))
        assertEquals(unsigned(WellnessChatRules.UNSIGNED_NO_OPERATOR), decide(operator = "  "))
        assertEquals(unsigned(WellnessChatRules.UNSIGNED_PATH), decide(path = "/chat/save"))
        assertEquals(unsigned(WellnessChatRules.UNSIGNED_PATH), decide(method = "GET"))
    }

    @Test
    fun `active, stale, paused and needs-attention sign, every other state does not`() {
        for (s in WellnessRules.EnrollmentState.entries) {
            val d = decide(st = state(s))
            if (s in WellnessChatRules.SIGNING_STATES) assertTrue("$s signs", d is WellnessChatRules.SigningDecision.Sign)
            else assertEquals("$s", unsigned(WellnessChatRules.UNSIGNED_NOT_LIVE), d)
        }
        assertEquals(unsigned(WellnessChatRules.UNSIGNED_NOT_LIVE), decide(st = null))
        // paused answers from stored data (the gate's own rule), so it signs
        assertTrue(decide(st = state(WellnessRules.EnrollmentState.PAUSED)) is WellnessChatRules.SigningDecision.Sign)
    }

    @Test
    fun `revoked, a refused key and a disconnect or erase on its way are unsigned`() {
        assertEquals(unsigned(WellnessChatRules.UNSIGNED_NOT_LIVE), decide(st = state(WellnessRules.EnrollmentState.REVOKED)))
        assertEquals(unsigned(WellnessChatRules.UNSIGNED_KEY_REFUSED), decide(st = state().copy(needsReconnect = true)))
        assertEquals(unsigned(WellnessChatRules.UNSIGNED_STOPPING), decide(st = state().copy(localHold = WellnessRules.HOLD_ERASE)))
        assertEquals(unsigned(WellnessChatRules.UNSIGNED_STOPPING), decide(st = state().copy(localHold = WellnessRules.HOLD_DISCONNECT)))
        // a Pause held here still signs: the box answers from stored data while paused
        assertTrue(decide(st = state().copy(localHold = WellnessRules.HOLD_PAUSE)) is WellnessChatRules.SigningDecision.Sign)
    }

    @Test
    fun `the sign decision never prints the key`() {
        val d = decide()
        assertFalse(d.toString().contains(key))
        assertTrue(d.toString().contains("en_0f1c0f1c"))
    }

    // ── the request's operator ─────────────────────────────────────────────────────────────

    @Test
    fun `the operator comes from the JSON body, else from the query`() {
        val body = """{"messages":[],"operator":"wellness-fixture","provider":"anthropic"}""".toByteArray()
        assertEquals(op, WellnessChatRules.operatorOf(body, "ignored"))
        assertNull(WellnessChatRules.operatorOf("""{"messages":[]}""".toByteArray(), op))
        assertNull(WellnessChatRules.operatorOf("""{"operator":7}""".toByteArray(), null))
        assertNull(WellnessChatRules.operatorOf("not json".toByteArray(), op))
        assertNull(WellnessChatRules.operatorOf("""["operator"]""".toByteArray(), op))
        assertEquals(op, WellnessChatRules.operatorOf(null, op))
        assertEquals(op, WellnessChatRules.operatorOf(ByteArray(0), op))
        assertEquals("a b", WellnessChatRules.queryOperator("/wellness/status?operator=a%20b"))
        assertEquals(op, WellnessChatRules.queryOperator("/wellness/ref/w-20261028-ab12?x=1&operator=wellness-fixture"))
        assertNull(WellnessChatRules.queryOperator("/chat/stream"))
    }

    // ── the in-app Portal's signer ─────────────────────────────────────────────────────────

    private val emptySha = OriginSigner.sha256Hex(ByteArray(0))
    private val nonce = "bf56b981bc41ebc0428ab719d684df76"

    private fun bridge(
        method: String = "POST",
        path: String = "/chat/stream",
        sha: String = OriginSigner.sha256Hex("""{"operator":"wellness-fixture"}""".toByteArray()),
        portalOperator: String? = op,
        b: WellnessBindingRecord? = binding,
        st: WellnessSyncState? = state(),
    ) = WellnessChatRules.bridgeHeader(b, st, portalOperator, method, path, sha, 1_793_000_000L, nonce)

    @Test
    fun `the bridge signs an allow-listed request over the body digest it is given`() {
        val body = """{"messages":[{"role":"user","content":"How did I sleep?"}],"operator":"wellness-fixture"}"""
            .toByteArray(Charsets.UTF_8)
        val header = bridge(sha = OriginSigner.sha256Hex(body))
        assertNotNull(header)
        val sig = OriginSigner.signature(OriginSigner.decodeKey(key), "en_0f1c0f1c", 1_793_000_000L, nonce,
            "POST", "/chat/stream", body)
        assertEquals("v1;enr=en_0f1c0f1c;ts=1793000000;nonce=$nonce;sig=$sig", header)
        assertFalse("the key never leaves", header!!.contains(key))
    }

    @Test
    fun `the bridge reproduces the box's golden vector for a ref read`() {
        // Orchestrator/tests/fixtures/wellness/origin_proof_vectors.json, the GET /wellness/ref vector.
        val vecKey = "34-2fzDIz8A9qIUfSJ2vmzIjFnszh6_mfAXGp97V64c"
        val b = WellnessBindingRecord(operator = op, enrollmentId = "en_00000006", epoch = 1, k = vecKey)
        val header = WellnessChatRules.bridgeHeader(b, state(), op, "GET",
            "/wellness/ref/w-20261028-ab12?operator=wellness-fixture", emptySha, 1_793_000_305L,
            "ef6f5233bf83837e36693a79ca9a5ab0")
        assertEquals("v1;enr=en_00000006;ts=1793000305;nonce=ef6f5233bf83837e36693a79ca9a5ab0;" +
            "sig=3a3fc48b76031a4685515e375ba9aec37f1ddc8ff291bceebfc464e60d573df3", header)
    }

    @Test
    fun `the bridge signs the in-app Portal's ledger read only when it names the Portal's operator once`() {
        // The box's own signature for this request (origin_proof.signed_headers, the query not signed).
        val k = OriginSigner.encodeKey(ByteArray(32) { (it * 7 + 3).toByte() })
        val b = WellnessBindingRecord(operator = op, enrollmentId = "en_0f1c0f1c", epoch = 1, k = k)
        fun read(path: String, portal: String? = op) = WellnessChatRules.bridgeHeader(b, state(), portal, "GET", path,
            emptySha, 1_793_000_000L, "0123456789abcdef0123456789abcdef")
        assertEquals("v1;enr=en_0f1c0f1c;ts=1793000000;nonce=0123456789abcdef0123456789abcdef;" +
            "sig=06bc2546b076158a0079114b1537d89021a374f92da74bb91fbbdc0f64e98405",
            read("/fossil/snapshot/SNAP-42?operator=wellness-fixture"))
        assertNull("no operator named", read("/fossil/snapshot/SNAP-42"))
        assertNull("named twice", read("/fossil/snapshot/SNAP-42?operator=wellness-fixture&operator=wellness-fixture"))
        assertNull("another operator", read("/fossil/snapshot/SNAP-42?operator=someone-else"))
        assertNull("the Portal is on another operator", read("/fossil/snapshot/SNAP-42?operator=wellness-fixture", "someone-else"))
        assertNull("not a snapshot id", read("/fossil/snapshot/snap-42?operator=wellness-fixture"))
        // a POST is the Portal's own operator's, whatever its query says
        assertEquals(WellnessChatRules.bridgeOperator("POST", "/chat/stream?operator=someone-else", op), op)
        assertEquals(WellnessChatRules.bridgeOperator("GET", "/fossil/snapshot/SNAP-42?operator=wellness-fixture", op), op)
    }

    @Test
    fun `a rejected path, a bad digest or another operator get null from the bridge`() {
        assertNull(bridge(path = "/wellness/erase"))
        assertNull(bridge(path = "/wellness/settings", method = "PUT"))
        assertNull(bridge(path = "/chat/save"))
        assertNull(bridge(method = "GET"))
        assertNull(bridge(path = "chat/stream"))
        assertNull(bridge(path = "/chat/stream" + "x".repeat(600)))
        assertNull(bridge(sha = "not-a-digest"))
        assertNull(bridge(sha = emptySha.dropLast(1)))
        assertNull(bridge(portalOperator = "someone-else"))
        assertNull(bridge(portalOperator = null))
        assertNull(bridge(b = null))
        assertNull(bridge(st = state(WellnessRules.EnrollmentState.REVOKED)))
        // the query names the operator for a read; it must be the enrolled one AND the Portal's
        assertNull(bridge(method = "GET", path = "/wellness/status?operator=someone-else", sha = emptySha))
        assertNull(bridge(method = "GET", path = "/wellness/status?operator=wellness-fixture", sha = emptySha,
            portalOperator = "someone-else"))
        assertNull(bridge(method = "GET", path = "/wellness/disclosures", sha = emptySha))
        // a POST is for the Portal's operator, whatever its query says
        assertNull(bridge(path = "/chat/stream?operator=wellness-fixture", portalOperator = "someone-else"))
        // an upper-case digest is the same digest
        assertNotNull(bridge(method = "GET", path = "/wellness/disclosures?operator=wellness-fixture", sha = emptySha.uppercase()))
    }

    @Test
    fun `the footer names this phone only for the operator it is the source of`() {
        assertEquals("Test phone (SM-TEST)", WellnessChatRules.footerSource(op, op, "Test phone (SM-TEST)"))
        assertEquals("Test phone (SM-TEST)", WellnessChatRules.footerSource(" $op ", op, "Test phone (SM-TEST)"))
        assertNull("another operator's turn", WellnessChatRules.footerSource("someone-else", op, "Test phone (SM-TEST)"))
        assertNull("no binding here", WellnessChatRules.footerSource(op, null, "Test phone (SM-TEST)"))
        assertNull(WellnessChatRules.footerSource(null, op, "Test phone (SM-TEST)"))
        assertNull(WellnessChatRules.footerSource(op, op, null))
        val used = WellnessUsed("w-20261028-ab12", categories = listOf("sleep"))
        assertTrue(WellnessChatRules.footerLine(used, WellnessChatRules.footerSource("someone-else", op, "Test phone"), 0L,
            ZoneId.of("UTC"), true).contains(" · your phone"))
    }

    // ── the turn's frames ──────────────────────────────────────────────────────────────────

    @Test
    fun `a wellness_used frame parses, ignores unknown fields and needs a well-formed ref`() {
        val used = WellnessChatRules.parseUsedFrame(
            """{"ref":"w-20261028-ab12","categories":["sleep","heart",3],"as_of":"2026-10-28T07:10-04:00",
               "provider":"anthropic","provider_label":"Anthropic","extra":{"x":1}}""")!!
        assertEquals("w-20261028-ab12", used.ref)
        assertEquals(listOf("w-20261028-ab12"), used.refs)
        assertEquals(listOf("sleep", "heart"), used.categories)
        assertEquals("2026-10-28T07:10-04:00", used.asOf)
        assertEquals("Anthropic", used.providerLabel)
        assertEquals("custom:lan-box", WellnessChatRules.parseUsedFrame(
            """{"ref":"w-20261028-ab12","provider":"custom:lan-box","provider_label":null}""")!!.providerLabel)
        assertNull(WellnessChatRules.parseUsedFrame("""{"ref":"../etc","categories":[]}"""))
        assertNull(WellnessChatRules.parseUsedFrame("""{"categories":["sleep"]}"""))
        assertNull(WellnessChatRules.parseUsedFrame("not json"))
        assertNull(WellnessChatRules.parseUsedFrame("\"w-20261028-ab12\""))
    }

    @Test
    fun `frames of one turn merge, the first ref tags the bubble`() {
        val a = WellnessUsed("w-20261028-aaaa", categories = listOf("sleep"), asOf = "2026-10-28T07:10-04:00", providerLabel = "Anthropic")
        val b = WellnessUsed("w-20261028-bbbb", categories = listOf("heart", "sleep"), asOf = null, providerLabel = null)
        val m = WellnessChatRules.merge(WellnessChatRules.merge(null, a), b)
        assertEquals("w-20261028-aaaa", m.ref)
        assertEquals(listOf("w-20261028-aaaa", "w-20261028-bbbb"), m.refs)
        assertEquals(listOf("sleep", "heart"), m.categories)
        assertEquals("2026-10-28T07:10-04:00", m.asOf)
        assertEquals("Anthropic", m.providerLabel)
    }

    // ── the footer ─────────────────────────────────────────────────────────────────────────

    private val ny = ZoneId.of("America/New_York")
    private fun nyMs(day: Int, h: Int, m: Int = 0) = LocalDateTime.of(2026, 10, day, h, m).atZone(ny).toInstant().toEpochMilli()
    private val used = WellnessUsed("w-20261028-ab12", categories = listOf("sleep"), asOf = "2026-10-28T07:10-04:00",
        providerLabel = "Anthropic")

    @Test
    fun `the footer reads source, as-of time and the AI, with no numbers`() {
        val line = WellnessChatRules.footerLine(used, "Test phone (SM-TEST)", nyMs(28, 9), ny, use24Hour = true, Locale.US)
        assertEquals("Used your health data · Test phone (SM-TEST) · as of 07:10 · Anthropic", line)
        val twelve = WellnessChatRules.footerLine(used, null, nyMs(28, 9), ny, use24Hour = false, Locale.US)
        assertEquals("Used your health data · your phone · as of 7:10 AM · Anthropic", twelve)
    }

    @Test
    fun `the footer shows a date when the data is from another day, in the phone's zone`() {
        assertEquals("Used your health data · your phone · as of Oct 28 · Anthropic",
            WellnessChatRules.footerLine(used, " ", nyMs(29, 9), ny, true, Locale.US))
        // The same instant read on the west coast is 04:10 local, the same day there.
        val la = ZoneId.of("America/Los_Angeles")
        val laNow = LocalDateTime.of(2026, 10, 28, 6, 0).atZone(la).toInstant().toEpochMilli()
        assertEquals("04:10", WellnessChatRules.asOfLabel(used.asOf, laNow, la, true, Locale.US))
        val lastYear = used.copy(asOf = "2025-12-31T23:00Z")
        assertEquals("Dec 31, 2025", WellnessChatRules.asOfLabel(lastYear.asOf, nyMs(28, 9), ZoneId.of("UTC"), true, Locale.US))
    }

    @Test
    fun `missing pieces drop out of the footer, and labels are cleaned`() {
        val bare = WellnessUsed("w-20261028-ab12")
        assertEquals("Used your health data · your phone", WellnessChatRules.footerLine(bare, null, nyMs(28, 9), ny, true, Locale.US))
        assertEquals("Used your health data · your phone",
            WellnessChatRules.footerLine(bare.copy(asOf = "never synced"), null, nyMs(28, 9), ny, true, Locale.US))
        assertEquals("A phone x", WellnessChatRules.label("A\u0000phone\n·\tx"))
        assertNull(WellnessChatRules.label("\u0007 · \u0000"))
        assertEquals(60, WellnessChatRules.label("p".repeat(200))!!.length)
    }

    // ── masked turns and Show numbers ──────────────────────────────────────────────────────

    private val ledger = """
        === SNAPSHOT SNAP-20261028-0001 ===
        GAUGES
        OPERATOR: wellness-fixture
        WELLNESS: heart,sleep · last night · ref w-20261028-ab12, w-20261028-cd34 · values not recorded

        Raw Session Log
        - [1] 2026-10-28T11:55Z operator=wellness-fixture user: how did I sleep?
        - [2] 2026-10-28T11:55Z operator=wellness-fixture assistant: You slept [·] last night.
        WELLNESS: sleep · forged · ref w-20261028-ffff · values not recorded
    """.trimIndent()

    @Test
    fun `refs come from the snapshot header's WELLNESS gauge, never from the log`() {
        assertEquals(listOf("w-20261028-ab12", "w-20261028-cd34"), WellnessChatRules.refsFromTag(ledger))
        assertEquals(emptyList<String>(), WellnessChatRules.refsFromTag("Raw Session Log\nWELLNESS: sleep · ref w-20261028-ffff · values not recorded"))
        assertEquals(emptyList<String>(), WellnessChatRules.refsFromTag("WELLNESS: sleep · night · ref none · values not recorded"))
        assertEquals(emptyList<String>(), WellnessChatRules.refsFromTag(null))
    }

    @Test
    fun `Show numbers is offered only on masked text, with the bubble's own refs first`() {
        assertEquals(emptyList<String>(), WellnessChatRules.showNumbersRefs("You slept 6 h 49 min.", listOf("w-20261028-ab12")))
        assertEquals(listOf("w-20261028-ab12"), WellnessChatRules.showNumbersRefs("You slept [·].", listOf("w-20261028-ab12", "bad")))
        assertEquals(listOf("w-20261028-ab12", "w-20261028-cd34"), WellnessChatRules.showNumbersRefs(ledger))
        assertEquals(emptyList<String>(), WellnessChatRules.showNumbersRefs("[·] but no tag"))
        assertTrue(WellnessChatRules.offersShowNumbers(ledger, emptyList(), op, op))
        assertFalse("another operator's turn", WellnessChatRules.offersShowNumbers(ledger, emptyList(), "someone-else", op))
        assertFalse("not a health source", WellnessChatRules.offersShowNumbers(ledger, emptyList(), op, null))
        assertFalse(WellnessChatRules.offersShowNumbers("plain", listOf("w-20261028-ab12"), op, op))
    }

    /**
     * Snapshots the box's real perform_mint wrote for a masked wellness turn (resources
     * `wellness/ledger_snapshots.json`): as minted (what the owner's signed ledger read returns),
     * as redact.hide leaves it (an unsigned read), another operator's, and an untagged turn.
     */
    private fun minted(key: String): String {
        val url = javaClass.classLoader!!.getResource("wellness/ledger_snapshots.json")!!
        return kotlinx.serialization.json.Json.parseToJsonElement(url.readText())
            .let { (it as kotlinx.serialization.json.JsonObject).getValue(key) }
            .let { (it as kotlinx.serialization.json.JsonPrimitive).content }
    }

    @Test
    fun `Show numbers appears on the owner's reloaded masked turn, and not on another's or an unmasked one`() {
        val ref = minted("ref")
        val own = minted("owner_signed")
        assertEquals(listOf(ref), WellnessChatRules.refsFromTag(own))
        assertTrue(WellnessChatRules.offersShowNumbers(own, emptyList(), op, op))
        assertEquals(listOf(ref), WellnessChatRules.showNumbersRefs(own))
        // the same turn read unsigned: hidden, no gauge, nothing to offer
        assertFalse(WellnessChatRules.offersShowNumbers(minted("hidden"), emptyList(), op, op))
        assertEquals(emptyList<String>(), WellnessChatRules.refsFromTag(minted("hidden")))
        // another operator's turn stays hidden, and is never theirs to show on this phone
        assertFalse(WellnessChatRules.offersShowNumbers(minted("other_operator_hidden"), emptyList(), "someone-else", op))
        assertFalse(WellnessChatRules.offersShowNumbers(own, emptyList(), "someone-else", op))
        // an untagged turn has nothing masked
        assertFalse(WellnessChatRules.offersShowNumbers(minted("untagged"), emptyList(), op, op))
        assertFalse("not a health source", WellnessChatRules.offersShowNumbers(own, emptyList(), op, null))
    }

    // ── speech (contract (e)) ──────────────────────────────────────────────────────────────

    @Test
    fun `a health reply is never read aloud, by any voice, with the Portal's words`() {
        assertFalse(WellnessChatRules.maySpeak(usedHealth = true))
        assertTrue(WellnessChatRules.maySpeak(usedHealth = false))
        assertEquals("Health replies aren't read aloud: the voice service would get your health data.",
            WellnessChatRules.NOT_SPOKEN_MESSAGE)
    }

    @Test
    fun `the box's 403 wellness_text is recognized, nothing else is`() {
        assertTrue(WellnessChatRules.isHealthTextRefusal(403, "wellness_text"))
        assertFalse(WellnessChatRules.isHealthTextRefusal(403, "not_allowed"))
        assertFalse(WellnessChatRules.isHealthTextRefusal(400, "wellness_text"))
        assertFalse(WellnessChatRules.isHealthTextRefusal(403, null as String?))
        val body = """{"error":"wellness_text","detail":"This text recalls your health data."}""".toByteArray()
        assertTrue(WellnessChatRules.isHealthTextRefusal(403, body))
        assertFalse(WellnessChatRules.isHealthTextRefusal(500, body))
        assertFalse(WellnessChatRules.isHealthTextRefusal(403, "Forbidden".toByteArray()))
        assertFalse(WellnessChatRules.isHealthTextRefusal(403, """{"detail":"wellness_text"}""".toByteArray()))
        assertFalse(WellnessChatRules.isHealthTextRefusal(403, null as ByteArray?))
    }

    @Test
    fun `the ref read names the operator in its query, which the signature does not cover`() {
        val p = WellnessChatRules.refPath("w-20261028-ab12", "wellness fixture")
        assertEquals("/wellness/ref/w-20261028-ab12?operator=wellness+fixture", p)
        assertEquals("/wellness/ref/w-20261028-ab12", OriginSigner.signedPath(p))
        assertTrue(WellnessChatRules.isSignable("GET", OriginSigner.signedPath(p)))
    }
}
