package com.aiblackbox.portal.data.wellness

import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.api.HttpClients
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
 * request of the enrolled operator carries X-BlackBox-Origin computed over the exact bytes the
 * box receives (the box's own golden vectors reproduce when ts and nonce are pinned);
 * everything else arrives exactly as a client without the interceptor sends it.
 */
class WellnessSigningInterceptorTest {

    private val server = MockWebServer()
    private val op = WellnessFixtures.OPERATOR
    private val json = "application/json; charset=utf-8".toMediaType()

    @Before fun setUp() = server.start()

    @After fun tearDown() = server.close()

    private fun facts(enr: String, k: String, state: WellnessRules.EnrollmentState = WellnessRules.EnrollmentState.ACTIVE) =
        WellnessSigningInterceptor.Facts(WellnessBindingRecord(operator = op, enrollmentId = enr, epoch = 1, k = k),
            WellnessSyncState(state = state.wire))

    private val k = OriginSigner.encodeKey(ByteArray(32) { (it * 7 + 3).toByte() })
    private val enrolled = facts("en_0f1c0f1c", k)

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

    private fun post(path: String, body: String) =
        Request.Builder().url(server.url(path)).header("X-BlackBox-Client", "native-android/1.0")
            .post(body.toRequestBody(json)).build()

    private fun get(pathWithQuery: String) = Request.Builder().url(server.url(pathWithQuery)).get().build()

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
            val req = send(c, get(v.s("path") + "?operator=$op"))
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
        assertUntouched(send(client(f = WellnessSigningInterceptor.Facts(null, WellnessSyncState())), r), today)
        assertUntouched(send(client(f = facts("en_0f1c0f1c", k, WellnessRules.EnrollmentState.REVOKED)), r), today)
        assertUntouched(send(client(f = facts("en_0f1c0f1c", k, WellnessRules.EnrollmentState.NOT_SET_UP)), r), today)
        val refused = WellnessSigningInterceptor.Facts(enrolled.binding, enrolled.state!!.copy(needsReconnect = true))
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
    fun `a BlackBoxApi call goes through the interceptor end to end`() = runBlocking {
        server.enqueue(MockResponse.Builder().code(200).body("{}").build())
        val api = BlackBoxApi(server.url("").toString().trimEnd('/'), client())
        api.post("/chat", chatBody())
        val req = server.takeRequest(5, TimeUnit.SECONDS)!!
        assertTrue(req.headers[OriginSigner.HEADER]!!.startsWith("v1;enr=en_0f1c0f1c;ts=1793000000;"))
    }
}
