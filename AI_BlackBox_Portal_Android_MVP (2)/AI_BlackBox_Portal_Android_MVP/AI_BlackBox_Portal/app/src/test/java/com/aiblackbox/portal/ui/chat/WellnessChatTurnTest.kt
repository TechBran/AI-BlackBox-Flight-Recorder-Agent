package com.aiblackbox.portal.ui.chat

import android.app.Application
import android.os.Looper
import androidx.compose.ui.text.input.TextFieldValue
import com.aiblackbox.portal.data.repository.ModelCatalogs
import com.aiblackbox.portal.data.store.BlackBoxStore
import com.aiblackbox.portal.data.store.ChatHistoryStore
import com.aiblackbox.portal.data.wellness.OriginSigner
import com.aiblackbox.portal.data.wellness.WellnessBindingRecord
import com.aiblackbox.portal.data.wellness.WellnessChatSigning
import com.aiblackbox.portal.data.wellness.WellnessFixtures
import com.aiblackbox.portal.data.wellness.WellnessRules
import com.aiblackbox.portal.data.wellness.WellnessSigningInterceptor
import com.aiblackbox.portal.data.wellness.WellnessSyncState
import com.aiblackbox.portal.data.wellness.WellnessUsed
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import mockwebserver3.Dispatcher
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import mockwebserver3.RecordedRequest
import okhttp3.Headers.Companion.headersOf
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import java.util.concurrent.ConcurrentLinkedQueue

/**
 * One health turn through the REAL ChatViewModel and the app's one HTTP client (spec §3.7,
 * §6.1 "Chat"): on the enrolled phone the /chat/stream request is signed over its exact body,
 * the turn key from stream_start is echoed to /chat/save, the reply bubble carries what the
 * turn used (the footer's source) and its ref, the local history keeps both, and the box gets
 * nothing else new. A phone that is no health source sends the same turn unsigned.
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class)
class WellnessChatTurnTest {

    private lateinit var server: MockWebServer
    private class Seen(val path: String, val body: ByteArray, val origin: String?)
    private val requests = ConcurrentLinkedQueue<Seen>()
    private val op = WellnessFixtures.OPERATOR
    private val key = ByteArray(32) { (it * 3 + 1).toByte() }

    private val stream = listOf(
        "event: stream_start\ndata: {\"provider\": \"anthropic\", \"model\": \"claude-test\", \"wellness_turn\": \"wt_0123456789abcdef\"}\n\n",
        "event: tool_start\ndata: \"wellness_summary\"\n\n",
        "event: tool_result\ndata: \"wellness_summary: health data read\"\n\n",
        "event: wellness_used\ndata: {\"ref\": \"w-20261028-ab12\", \"categories\": [\"sleep\"], \"as_of\": \"2026-10-28T07:10-04:00\", \"provider\": \"anthropic\", \"provider_label\": \"Anthropic\"}\n\n",
        "event: content_start\ndata: \"\"\n\n",
        "event: content\ndata: \"You slept a little less than usual.\"\n\n",
        "event: done\ndata: {\"content\": \"You slept a little less than usual.\", \"reasoning\": \"\"}\n\n",
        "event: stream_end\ndata: \"\"\n\n",
    ).joinToString("")

    @Before fun setUp() {
        ModelCatalogs.clear()
        server = MockWebServer()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val path = request.target.substringBefore('?')
                requests += Seen(path, request.body?.toByteArray() ?: ByteArray(0), request.headers[OriginSigner.HEADER])
                fun ok(body: String) = MockResponse.Builder().code(200)
                    .headers(headersOf("Content-Type", "application/json")).body(body).build()
                return when (path) {
                    "/chat/stream" -> MockResponse.Builder().code(200)
                        .headers(headersOf("Content-Type", "text/event-stream")).body(stream).build()
                    "/chat/save" -> ok("""{"status":"ok"}""")
                    "/operators" -> ok("""{"operators":["$op"],"default":"$op"}""")
                    "/health" -> ok("""{"status":"ok","snapshot_count":7}""")
                    "/tasks/list" -> ok("""{"tasks":[]}""")
                    else -> ok(if (path.startsWith("/models/")) """{"models":[],"source":"live"}""" else "{}")
                }
            }
        }
        server.start()
    }

    @After fun tearDown() {
        WellnessChatSigning.installForTest(null)
        server.close()
        ModelCatalogs.clear()
    }

    private fun pumpUntil(what: String, timeoutMs: Long = 15_000, done: () -> Boolean) {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            shadowOf(Looper.getMainLooper()).idle()
            if (done()) return
            Thread.sleep(5)
        }
        fail("timed out waiting for $what")
    }

    private fun enrolled() = WellnessSigningInterceptor.Facts(
        WellnessBindingRecord(operator = op, enrollmentId = "en_0f1c0f1c", epoch = 1, k = OriginSigner.encodeKey(key)),
        WellnessSyncState(state = WellnessRules.EnrollmentState.ACTIVE.wire, sourceLabel = "Test phone (SM-TEST)"),
        server.url("").toString(),
    )

    private fun runTurn(): ChatViewModel {
        val app = RuntimeEnvironment.getApplication()
        val store = BlackBoxStore(app)
        runBlocking {
            store.setOperator(op)
            store.setProvider("anthropic")
            store.setModel("")
        }
        val vm = ChatViewModel(app)
        vm.initialize(server.url("").toString().trimEnd('/'))
        pumpUntil("the operator to resolve") { vm.operators.value == listOf(op) }
        vm.onInputChange(TextFieldValue("How did I sleep last night?"))
        vm.sendMessage()
        pumpUntil("the turn and its save") {
            val s = vm.chatState.value
            s != ChatState.STREAMING && s != ChatState.THINKING && vm.messages.value.none { it.isStreaming } &&
                requests.any { it.path == "/chat/save" }
        }
        return vm
    }

    @Test
    fun `on the enrolled phone the turn is signed, its key saved and its use kept on the bubble`() {
        WellnessChatSigning.installForTest { enrolled() }
        val vm = runTurn()

        val sent = requests.first { it.path == "/chat/stream" }
        assertNotNull("the turn is signed", sent.origin)
        val origin = sent.origin!!
        val f = origin.removePrefix("v1;").split(';').associate { it.substringBefore('=') to it.substringAfter('=') }
        assertEquals("en_0f1c0f1c", f["enr"])
        assertEquals(OriginSigner.signature(key, "en_0f1c0f1c", f.getValue("ts").toLong(), f.getValue("nonce"), "POST",
            "/chat/stream", sent.body), f["sig"])
        val streamBody = sent.body.toString(Charsets.UTF_8)
        for (k in listOf("wellness_ref", "wellness_used", "wellness_turn", "wellnessUsed", "wellnessRef")) {
            assertFalse("the stream request carries nothing new ($k)", streamBody.contains(k))
        }

        val save = Json.parseToJsonElement(requests.first { it.path == "/chat/save" }.body.toString(Charsets.UTF_8)).jsonObject
        assertEquals("wt_0123456789abcdef", save["wellness_turn"]?.jsonPrimitive?.content)
        assertNull("the save is not a signed path", requests.first { it.path == "/chat/save" }.origin)

        val reply = vm.messages.value.last()
        assertEquals("assistant", reply.role)
        assertEquals("You slept a little less than usual.", reply.content)
        assertEquals("w-20261028-ab12", reply.wellnessRef)
        assertEquals(WellnessUsed("w-20261028-ab12", listOf("w-20261028-ab12"), listOf("sleep"), "2026-10-28T07:10-04:00",
            "Anthropic"), reply.wellnessUsed)

        val history = ChatHistoryStore(RuntimeEnvironment.getApplication())
        pumpUntil("the turn in the local history") {
            runBlocking { history.load(op) }.any { it.role == "assistant" && it.wellnessUsed != null }
        }
        val persisted = runBlocking { history.load(op) }.last { it.role == "assistant" }
        assertEquals(reply.wellnessUsed, persisted.wellnessUsed)
        assertEquals("w-20261028-ab12", persisted.wellnessRef)
    }

    @Test
    fun `a phone that is no health source sends the same turn unsigned, and still echoes the key`() {
        WellnessChatSigning.installForTest { WellnessSigningInterceptor.Facts(null, null, server.url("").toString()) }
        runTurn()
        assertNull(requests.first { it.path == "/chat/stream" }.origin)
        val save = Json.parseToJsonElement(requests.first { it.path == "/chat/save" }.body.toString(Charsets.UTF_8)).jsonObject
        assertEquals("wt_0123456789abcdef", save["wellness_turn"]?.jsonPrimitive?.content)
    }
}
