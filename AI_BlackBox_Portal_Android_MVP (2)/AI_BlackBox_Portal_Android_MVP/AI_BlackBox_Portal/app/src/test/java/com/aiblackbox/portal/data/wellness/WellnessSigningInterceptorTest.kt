package com.aiblackbox.portal.data.wellness

import com.aiblackbox.portal.PortalWebGuard
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.api.HttpClients
import com.aiblackbox.portal.data.api.SSEClient
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import mockwebserver3.RecordedRequest
import okhttp3.MediaType
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okio.BufferedSink
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * The chat-signing interceptor over a real HTTP hop to a stand-in box: an allow-listed
 * request of the enrolled operator that opts in, to the paired box, carries X-BlackBox-Origin
 * computed over the exact bytes the box receives (the box's own golden vectors reproduce when
 * ts and nonce are pinned); everything else (no opt-in, another host, the WebView's download
 * client, the overlay's /chat) arrives exactly as a client without the interceptor sends it.
 */
class WellnessSigningInterceptorTest {

    private val server = MockWebServer()
    private val op = WellnessFixtures.OPERATOR
    private val json = "application/json; charset=utf-8".toMediaType()

    @Before fun setUp() = server.start()

    @After fun tearDown() = server.close()

    /** A binding the stand-in box delivered (pinned to it), with the app paired with that box. */
    private fun facts(enr: String, k: String, state: WellnessRules.EnrollmentState = WellnessRules.EnrollmentState.ACTIVE) =
        WellnessSigningInterceptor.Facts(
            WellnessBindingRecord(operator = op, enrollmentId = enr, epoch = 1, k = k, boxOrigin = server.url("").toString()),
            WellnessSyncState(state = state.wire), server.url("").toString())

    private val k = OriginSigner.encodeKey(ByteArray(32) { (it * 7 + 3).toByte() })
    private val enrolled by lazy { facts("en_0f1c0f1c", k) }

    private fun client(
        f: WellnessSigningInterceptor.Facts? = enrolled,
        nowSec: Long = 1_793_000_000L,
        nonce: (() -> String)? = { "0123456789abcdef0123456789abcdef" },
    ): OkHttpClient = OkHttpClient.Builder()
        .addInterceptor(if (nonce == null) WellnessSigningInterceptor({ f }, { nowSec })
            else WellnessSigningInterceptor({ f }, { nowSec }, nonce))
        .build()

    private fun send(c: OkHttpClient, request: Request): RecordedRequest {
        server.enqueue(MockResponse.Builder().code(200).body("{}").build())
        c.newCall(request).execute().close()
        return server.takeRequest(5, TimeUnit.SECONDS)!!
    }

    /** A request as the native chat builds it: opted in to the credential. */
    private fun post(path: String, body: String, optIn: Boolean = true) =
        Request.Builder().url(server.url(path)).header("X-BlackBox-Client", "native-android/1.0")
            .let { if (optIn) WellnessSigningInterceptor.optIn(it) else it }
            .post(body.toRequestBody(json)).build()

    private fun get(pathWithQuery: String, optIn: Boolean = true, screen: String? = null) = Request.Builder().url(server.url(pathWithQuery))
        .let { if (optIn) WellnessSigningInterceptor.optIn(it, screen) else it }.get().build()

    private fun fields(header: String): Map<String, String> =
        header.removePrefix("v1;").split(';').associate { it.substringBefore('=') to it.substringAfter('=') }

    private fun chatBody(operator: String = op) =
        """{"messages":[{"role":"user","content":"How did I sleep last night?","timestamp":1}],"operator":"$operator","provider":"anthropic"}"""

    // ── the box's golden vectors ───────────────────────────────────────────────────────────

    private fun vectors(): List<JsonObject> {
        var dir: File? = File("").absoluteFile
        repeat(10) {
            val d = dir ?: return@repeat
            val f = File(d, "Orchestrator/tests/fixtures/wellness/origin_proof_vectors.json")
            if (f.exists()) return Json.parseToJsonElement(f.readText()).jsonArray.map { it.jsonObject }
            dir = d.parentFile
        }
        throw AssertionError("origin_proof_vectors.json not found above ${File("").absolutePath}")
    }

    private fun JsonObject.s(key: String) = getValue(key).jsonPrimitive.content

    @Test
    fun `the header the interceptor emits reproduces the box's vectors for every allow-listed read`() {
        val signable = vectors().filter { WellnessChatRules.isSignable(it.s("method"), it.s("path")) && it.s("body_utf8").isEmpty() }
        assertTrue("at least the status and the ref vectors", signable.size >= 2)
        for (v in signable) {
            val c = client(facts(v.s("enr"), v.s("k_b64url")), nowSec = v.s("ts").toLong(), nonce = { v.s("nonce") })
            // The operator in the query (the box's reads) or, for a ledger read, the one on screen.
            val req = send(c, get(v.s("path") + "?operator=$op", screen = op))
            val header = req.headers[OriginSigner.HEADER]!!
            assertEquals("v1;enr=${v.s("enr")};ts=${v.s("ts")};nonce=${v.s("nonce")};sig=${v.s("expected_sig")}", header)
        }
    }

    // ── a signed chat turn ─────────────────────────────────────────────────────────────────

    @Test
    fun `a chat stream POST is signed over exactly the bytes the box receives`() {
        val body = chatBody()
        val req = send(client(), post("/chat/stream", body))
        val received = req.body!!.toByteArray()
        assertTrue("the body is sent unchanged", received.contentEquals(body.toByteArray(Charsets.UTF_8)))
        assertEquals("application/json; charset=utf-8", req.headers["Content-Type"])
        assertEquals("native-android/1.0", req.headers["X-BlackBox-Client"])
        val f = fields(req.headers[OriginSigner.HEADER]!!)
        assertEquals("en_0f1c0f1c", f["enr"])
        assertEquals("1793000000", f["ts"])
        assertEquals(OriginSigner.signature(OriginSigner.decodeKey(k), "en_0f1c0f1c", 1_793_000_000L, f.getValue("nonce"),
            "POST", req.url.encodedPath, received), f["sig"])
    }

    @Test
    fun `chat and the health reads sign, with the operator from the body or the query`() {
        assertTrue(send(client(), post("/chat", chatBody())).headers[OriginSigner.HEADER] != null)
        assertTrue(send(client(), get("/wellness/disclosures?operator=$op")).headers[OriginSigner.HEADER] != null)
        assertTrue(send(client(), get(WellnessChatRules.refPath("w-20261028-ab12", op))).headers[OriginSigner.HEADER] != null)
        assertNull(send(client(), get("/wellness/ref/w-20261028-ab12")).headers[OriginSigner.HEADER])
        assertNull(send(client(), get("/wellness/status?operator=someone-else")).headers[OriginSigner.HEADER])
    }

    @Test
    fun `the nonce is fresh on every request`() {
        val c = client(nonce = null)
        val a = fields(send(c, post("/chat/stream", chatBody())).headers[OriginSigner.HEADER]!!).getValue("nonce")
        val b = fields(send(c, post("/chat/stream", chatBody())).headers[OriginSigner.HEADER]!!).getValue("nonce")
        assertTrue(OriginSigner.isWellFormedNonce(a) && OriginSigner.isWellFormedNonce(b))
        assertNotEquals(a, b)
    }

    // ── everything else is untouched ───────────────────────────────────────────────────────

    /** The same request through a client without the interceptor: what "today" sends. */
    private fun plain(request: Request): RecordedRequest = send(OkHttpClient(), request)

    private fun assertUntouched(viaInterceptor: RecordedRequest, viaPlain: RecordedRequest) {
        assertNull(viaInterceptor.headers[OriginSigner.HEADER])
        assertEquals(viaPlain.headers.toList(), viaInterceptor.headers.toList())
        assertTrue((viaPlain.body?.toByteArray() ?: ByteArray(0)).contentEquals(viaInterceptor.body?.toByteArray() ?: ByteArray(0)))
    }

    @Test
    fun `another operator's turn goes out byte-identical and unsigned`() {
        val r = post("/chat/stream", chatBody("someone-else"))
        assertUntouched(send(client(), r), plain(r))
    }

    @Test
    fun `no binding, a revoked enrollment, a refused key or no Health wiring sign nothing`() {
        val r = post("/chat/stream", chatBody())
        val today = plain(r)
        assertUntouched(send(client(f = null), r), today)
        assertUntouched(send(client(f = WellnessSigningInterceptor.Facts(null, WellnessSyncState(), server.url("").toString())), r), today)
        assertUntouched(send(client(f = facts("en_0f1c0f1c", k, WellnessRules.EnrollmentState.REVOKED)), r), today)
        assertUntouched(send(client(f = facts("en_0f1c0f1c", k, WellnessRules.EnrollmentState.NOT_SET_UP)), r), today)
        val refused = WellnessSigningInterceptor.Facts(enrolled.binding, enrolled.state!!.copy(needsReconnect = true), enrolled.boxOrigin)
        assertUntouched(send(client(f = refused), r), today)
    }

    @Test
    fun `a path off the list is never signed, whatever it carries`() {
        for (path in listOf("/chat/save", "/wellness/settings", "/wellness/erase", "/upload", "/health")) {
            val r = post(path, chatBody())
            assertUntouched(send(client(), r), plain(r))
        }
    }

    @Test
    fun `an already signed request keeps its own signature`() {
        val r = post("/chat/stream", chatBody()).newBuilder().header(OriginSigner.HEADER, "v1;enr=en_00000001;ts=1;nonce=x;sig=y").build()
        assertEquals("v1;enr=en_00000001;ts=1;nonce=x;sig=y", send(client(), r).headers[OriginSigner.HEADER])
    }

    @Test
    fun `a gzip, chunked or one-shot body is sent unsigned and intact`() {
        val gz = post("/chat/stream", chatBody()).newBuilder().header("Content-Encoding", "gzip").build()
        assertUntouched(send(client(), gz), plain(gz))

        val bytes = chatBody().toByteArray()
        fun body(oneShot: Boolean, length: Long) = object : RequestBody() {
            override fun contentType(): MediaType = json
            override fun contentLength(): Long = length
            override fun isOneShot(): Boolean = oneShot
            override fun writeTo(sink: BufferedSink) { sink.write(bytes) }
        }
        val oneShot = send(client(), Request.Builder().url(server.url("/chat/stream")).post(body(true, bytes.size.toLong())).build())
        assertNull(oneShot.headers[OriginSigner.HEADER])
        assertTrue(oneShot.body!!.toByteArray().contentEquals(bytes))
        val chunked = send(client(), Request.Builder().url(server.url("/chat/stream")).post(body(false, -1)).build())
        assertNull(chunked.headers[OriginSigner.HEADER])
        assertTrue(chunked.body!!.toByteArray().contentEquals(bytes))
    }

    // ── only the surfaces that opt in, and only to the paired box ─────────────────────────

    @Test
    fun `a request that does not opt in goes out unsigned and byte-identical, the overlay's chat among them`() {
        for (r in listOf(post("/chat", chatBody(), optIn = false), post("/chat/stream", chatBody(), optIn = false),
                get(WellnessChatRules.refPath("w-20261028-ab12", op), optIn = false),
                get("/wellness/status?operator=$op", optIn = false))) {
            assertUntouched(send(client(), r), plain(r))
        }
    }

    @Test
    fun `an opted-in request to a host other than the paired box is never signed`() {
        val other = MockWebServer().also { it.start() }
        try {
            val f = WellnessSigningInterceptor.Facts(enrolled.binding, enrolled.state, other.url("").toString())
            val r = post("/chat/stream", chatBody())
            assertUntouched(send(client(f = f), r), plain(r))
            val unpaired = WellnessSigningInterceptor.Facts(enrolled.binding, enrolled.state, null)
            assertUntouched(send(client(f = unpaired), r), plain(r))
            assertTrue(WellnessChatRules.sameOrigin("http://box.invalid:9091/chat", "http://box.invalid:9091/"))
            assertTrue(!WellnessChatRules.sameOrigin("http://box.invalid:9092/chat", "http://box.invalid:9091"))
            assertTrue(!WellnessChatRules.sameOrigin("https://box.invalid:9091/chat", "http://box.invalid:9091"))
            assertTrue(!WellnessChatRules.sameOrigin("http://box.invalid.evil:9091/chat", "http://box.invalid:9091"))
            assertTrue(!WellnessChatRules.sameOrigin("not a url", "http://box.invalid:9091"))
        } finally {
            other.close()
        }
    }

    @Test
    fun `the WebView's download client never signs a health read the page picks`() {
        WellnessChatSigning.installForTest { enrolled }
        try {
            val origin = server.url("").toString().trimEnd('/')
            val download = PortalWebGuard.pairedOnlyDownloadClient(PortalWebGuard.originOf(origin))
            assertTrue("it is derived from the base", WellnessChatSigning.interceptor in download.interceptors)
            for (path in listOf(WellnessChatRules.refPath("w-20261028-ab12", op), "/wellness/status?operator=$op",
                    "/wellness/disclosures?operator=$op")) {
                assertNull(path, send(download, Request.Builder().url(origin + path).build()).headers[OriginSigner.HEADER])
            }
        } finally {
            WellnessChatSigning.installForTest(null)
        }
    }

    // ── the ledger read (contract (a)) ─────────────────────────────────────────────────────

    /** A ledger read as the Timeline builds it: opted in, for the operator on screen, no query. */
    private fun ledger(snapId: String, screen: String? = op, optIn: Boolean = true) =
        Request.Builder().url(server.url("/fossil/snapshot/$snapId"))
            .let { if (optIn) WellnessSigningInterceptor.optIn(it, screen) else it }.get().build()

    @Test
    fun `the ledger read is signed for the enrolled operator on screen, as the box signs it`() {
        // The box's own header for this request (origin_proof.signed_headers, same key, ts and nonce).
        val req = send(client(), ledger("SNAP-20261028-42"))
        assertEquals("v1;enr=en_0f1c0f1c;ts=1793000000;nonce=0123456789abcdef0123456789abcdef;" +
            "sig=8d17c9c953a4e977cd45415c6dfbd5c3cb1c1fb45daf6cf336d1ac192a0dd49a", req.headers[OriginSigner.HEADER])
        assertEquals("/fossil/snapshot/SNAP-20261028-42", req.target)
    }

    @Test
    fun `a ledger read for anyone else on screen, or for nobody, goes out unsigned and byte-identical`() {
        for (r in listOf(ledger("SNAP-42", screen = "someone-else"), ledger("SNAP-42", screen = null),
                ledger("SNAP-42", optIn = false), ledger("snap-42"))) {
            assertUntouched(send(client(), r), plain(r))
        }
        // a query naming someone else makes it nobody's (the native app never sends one)
        val queried = Request.Builder().url(server.url("/fossil/snapshot/SNAP-42?operator=someone-else"))
            .let { WellnessSigningInterceptor.optIn(it, op) }.get().build()
        assertUntouched(send(client(), queried), plain(queried))
        // the screen operator never stands in for the operator a chat body or a read's query names
        val status = get("/wellness/status").newBuilder().also { WellnessSigningInterceptor.optIn(it, op) }.build()
        assertUntouched(send(client(), status), plain(status))
    }

    // ── the key's box (contract (f)) ───────────────────────────────────────────────────────

    @Test
    fun `an app re-pointed at another box sends it no proof, the same operator name or not`() {
        // The binding was delivered by another box; the app is now paired with this one.
        val elsewhere = WellnessSigningInterceptor.Facts(
            enrolled.binding!!.copy(boxOrigin = "http://100.64.0.99:9091"), enrolled.state, server.url("").toString())
        for (r in listOf(post("/chat/stream", chatBody()), post("/chat", chatBody()), ledger("SNAP-42"),
                get(WellnessChatRules.refPath("w-20261028-ab12", op)), get("/wellness/status?operator=$op"))) {
            assertUntouched(send(client(f = elsewhere), r), plain(r))
        }
    }

    @Test
    fun `a binding older than box_origin signs only once it has adopted the paired box`() {
        val legacy = enrolled.binding!!.copy(boxOrigin = null)
        val adopted = mutableListOf<String>()
        val adopts = WellnessSigningInterceptor.Facts(legacy, enrolled.state, server.url("").toString(),
            adoptOrigin = { adopted += it; true })
        assertTrue(send(client(f = adopts), post("/chat/stream", chatBody())).headers[OriginSigner.HEADER] != null)
        assertEquals(listOf(WellnessChatRules.originOf(server.url("").toString())), adopted)
        // the adoption did not stick (an unwritable binding): nothing is signed
        val refuses = WellnessSigningInterceptor.Facts(legacy, enrolled.state, server.url("").toString(), adoptOrigin = { false })
        val r = post("/chat/stream", chatBody())
        assertUntouched(send(client(f = refuses), r), plain(r))
        // and the default (no way to adopt) signs nothing either
        assertUntouched(send(client(f = WellnessSigningInterceptor.Facts(legacy, enrolled.state, server.url("").toString())), r), plain(r))
    }

    @Test
    fun `through the real binding an older record is pinned on its first signed use, rewritten once`() {
        val files = WellnessFiles(WellnessFixtures.tempDir("pin"))
        val binding = WellnessBinding(files)
        val intent = binding.beginIntent(op) // an older build: the intent names no box
        assertTrue(binding.acceptBind(WellnessBindRequest(op, "en_0f1c0f1c", 1, intent.nonce, k)) is BindDecision.Accepted)
        assertNull(binding.current()!!.boxOrigin)
        var paired = server.url("").toString()
        val live = { WellnessSigningInterceptor.Facts(binding.current(), enrolled.state, paired, adoptOrigin = binding::adoptBoxOrigin) }
        val c = OkHttpClient.Builder().addInterceptor(WellnessSigningInterceptor(live, { 1_793_000_000L })).build()

        assertTrue(send(c, post("/chat/stream", chatBody())).headers[OriginSigner.HEADER] != null)
        assertEquals(WellnessChatRules.originOf(paired), binding.current()!!.boxOrigin)
        val written = files.binding.readText()
        assertTrue(send(c, post("/chat/stream", chatBody())).headers[OriginSigner.HEADER] != null)
        assertEquals("rewritten once", written, files.binding.readText())

        // Re-pointed at another box now: the pinned binding signs nothing for it, and stays pinned.
        val other = MockWebServer().also { it.start() }
        try {
            paired = other.url("").toString()
            other.enqueue(MockResponse.Builder().code(200).body("{}").build())
            c.newCall(Request.Builder().url(other.url("/chat/stream")).let { WellnessSigningInterceptor.optIn(it) }
                .post(chatBody().toRequestBody(json)).build()).execute().close()
            assertNull(other.takeRequest(5, TimeUnit.SECONDS)!!.headers[OriginSigner.HEADER])
            assertEquals(WellnessChatRules.originOf(server.url("").toString()), binding.current()!!.boxOrigin)
            assertEquals(written, files.binding.readText())
        } finally {
            other.close()
        }
        assertTrue("the record stays in the no-backup tree", files.binding.canonicalPath.startsWith(files.root.canonicalPath))
    }

    // ── the app's clients ──────────────────────────────────────────────────────────────────

    @Test
    fun `every client the app derives from the base carries the one interceptor`() {
        assertTrue(WellnessChatSigning.interceptor in HttpClients.base.interceptors)
        val api = BlackBoxApi("http://localhost:9091")
        for (c in listOf(api.getClient(), api.streamClient, api.getTtsClient())) {
            assertTrue(WellnessChatSigning.interceptor in c.interceptors)
        }
    }

    @Test
    fun `a BlackBoxApi call that opts in goes through the interceptor end to end, one that does not stays plain`() = runBlocking {
        val api = BlackBoxApi(server.url("").toString().trimEnd('/'), client())
        server.enqueue(MockResponse.Builder().code(200).body("{}").build())
        api.post("/chat", chatBody(), signHealth = true)
        assertTrue(server.takeRequest(5, TimeUnit.SECONDS)!!.headers[OriginSigner.HEADER]!!
            .startsWith("v1;enr=en_0f1c0f1c;ts=1793000000;"))
        server.enqueue(MockResponse.Builder().code(200).body("{}").build())
        api.post("/chat", chatBody())
        assertNull(server.takeRequest(5, TimeUnit.SECONDS)!!.headers[OriginSigner.HEADER])
        server.enqueue(MockResponse.Builder().code(200).body("{}").build())
        api.get(WellnessChatRules.refPath("w-20261028-ab12", op))
        assertNull(server.takeRequest(5, TimeUnit.SECONDS)!!.headers[OriginSigner.HEADER])
    }

    @Test
    fun `the native chat's stream asks for the credential, a bare SSE stream does not`() = runBlocking {
        val api = BlackBoxApi(server.url("").toString().trimEnd('/'), client())
        val done = "event: done\ndata: {}\n\n"
        server.enqueue(MockResponse.Builder().code(200).body(done).build())
        com.aiblackbox.portal.data.repository.ChatRepository(api).sendStream("How did I sleep last night?", emptyList(), op).toList()
        assertTrue(server.takeRequest(5, TimeUnit.SECONDS)!!.headers[OriginSigner.HEADER] != null)
        server.enqueue(MockResponse.Builder().code(200).body(done).build())
        SSEClient(api).stream("/chat/stream", chatBody()).toList()
        assertNull(server.takeRequest(5, TimeUnit.SECONDS)!!.headers[OriginSigner.HEADER])
    }
}
