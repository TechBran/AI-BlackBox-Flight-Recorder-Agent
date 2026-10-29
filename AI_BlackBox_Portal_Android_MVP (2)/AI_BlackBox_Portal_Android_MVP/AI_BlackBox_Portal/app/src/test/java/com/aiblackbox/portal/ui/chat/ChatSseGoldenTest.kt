package com.aiblackbox.portal.ui.chat

import android.app.Application
import android.os.Looper
import androidx.compose.ui.text.input.TextFieldValue
import org.robolectric.RuntimeEnvironment
import com.aiblackbox.portal.data.model.ChatProvider
import com.aiblackbox.portal.data.model.UiMessage
import com.aiblackbox.portal.data.repository.ModelCatalogs
import com.aiblackbox.portal.data.store.BlackBoxStore
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import mockwebserver3.Dispatcher
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import mockwebserver3.RecordedRequest
import okhttp3.Headers.Companion.headersOf
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import java.io.File
import java.util.concurrent.ConcurrentLinkedQueue

/**
 * GOLDEN contract for the chat's /chat/stream path, end to end through the REAL
 * ChatViewModel (audit A04-chat-viewmodel-17/-18, rank 76). Recorded on the view model
 * BEFORE its SSE event switch was extracted into a pure reducer, and kept passing after,
 * so the extraction cannot change what a turn leaves behind.
 *
 * Each fixture (src/test/resources/sse/<name>.sse, written by gen_fixtures.py in the
 * exact frame format chat_routes.py generate_sse() emits) is served as the /chat/stream
 * body by a MockWebServer; the view model runs on Robolectric's main looper. What the turn
 * leaves is compared with <name>.golden.json: the turn's messages (content, reasoning,
 * model, tokens, provenance, media, retry flag), the chat state, the CU and robotics
 * state, the Signal line, the /chat/stream request and the /chat/save body (or none).
 *
 * The fixtures run in order in one view model, as turns of one conversation: the CU
 * session the computer_use turn opens is the one the next CU turns are sent with.
 *
 * Recording: a fixture with no golden file fails and writes the observed transcript to
 * app/build/sse-golden/<name>.golden.json to review and copy next to the fixture.
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class)
class ChatSseGoldenTest {

    private lateinit var server: MockWebServer
    private val requests = ConcurrentLinkedQueue<Pair<String, String>>() // (path, body)
    @Volatile private var streamBody: String = ""

    private val json = Json { prettyPrint = true; prettyPrintIndent = "  " }

    private val order = listOf(
        "anthropic_retrieval", "gemini_thinking", "openai_tool_image", "xai_structured_tool",
        "media_tasks", "error_only", "error_then_content", "done_fills_gaps", "empty_completion",
        "computer_use", "cu_queued", "cu_queue_next", "robotics_mission",
    )

    @Before fun setUp() {
        ModelCatalogs.clear()
        server = MockWebServer()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val path = request.target.substringBefore('?')
                requests += path to (request.body?.utf8() ?: "")
                fun ok(body: String) = MockResponse.Builder().code(200)
                    .headers(headersOf("Content-Type", "application/json")).body(body).build()
                return when {
                    path == "/chat/stream" -> MockResponse.Builder().code(200)
                        .headers(headersOf("Content-Type", "text/event-stream")).body(streamBody).build()
                    // No snap_id: the mint flourish would hold the Signal line for 1.6 s.
                    path == "/chat/save" -> ok("""{"status":"ok"}""")
                    path.startsWith("/tasks/status/") -> {
                        val id = path.removePrefix("/tasks/status/")
                        ok("""{"task_id":"$id","task_type":"image","status":"completed","progress":100,"result_url":"/ui/uploads/$id.png"}""")
                    }
                    path == "/operators" -> ok("""{"operators":["golden-op"],"default":"golden-op"}""")
                    path == "/health" -> ok("""{"status":"ok","snapshot_count":7}""")
                    path == "/tasks/list" -> ok("""{"tasks":[]}""")
                    path.startsWith("/models/") -> ok("""{"models":[],"source":"live"}""")
                    else -> ok("{}")
                }
            }
        }
        server.start()
    }

    @After fun tearDown() {
        server.close()
        ModelCatalogs.clear()
    }

    private fun resource(name: String): String =
        javaClass.classLoader!!.getResource("sse/$name")?.readText() ?: error("missing fixture sse/$name")

    /** Run the main looper (and let background threads post to it) until [done] or fail. */
    private fun pumpUntil(what: String, timeoutMs: Long = 15_000, done: () -> Boolean) {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            shadowOf(Looper.getMainLooper()).idle()
            if (done()) return
            Thread.sleep(5)
        }
        fail("timed out waiting for $what")
    }

    /** Pump for [ms] of wall time: lets a save or a media resolve that follows a turn land. */
    private fun settle(ms: Long = 400) {
        val until = System.currentTimeMillis() + ms
        while (System.currentTimeMillis() < until) {
            shadowOf(Looper.getMainLooper()).idle()
            Thread.sleep(5)
        }
    }

    private fun msgJson(m: UiMessage, base: String): JsonObject = buildJsonObject {
        put("role", m.role)
        put("content", m.content)
        put("reasoning", m.reasoning)
        put("model", m.model)
        put("provider", m.provider)
        put("isStreaming", m.isStreaming)
        put("isThinking", m.isThinking)
        put("sendFailed", m.sendFailed)
        // (if/else, not ?.let ?: — JsonObjectBuilder.put returns the PREVIOUS value, null.)
        val t = m.tokens
        if (t != null) put("tokens", buildJsonObject { put("prompt", t.prompt); put("completion", t.completion) })
        else put("tokens", JsonNull)
        val p = m.provenance
        if (p != null) {
            put("provenance", buildJsonObject {
                put("recent", buildJsonArray { p.recent.forEach { add(JsonPrimitive(it)) } })
                put("keyword", buildJsonArray { p.keyword.forEach { add(JsonPrimitive(it)) } })
                put("semantic", buildJsonArray { p.semantic.forEach { add(JsonPrimitive(it)) } })
                put("checkpoint", buildJsonArray { p.checkpoint.forEach { add(JsonPrimitive(it)) } })
            })
        } else {
            put("provenance", JsonNull)
        }
        put("mediaTasks", buildJsonArray { m.mediaTasks.forEach { add(JsonPrimitive(it)) } })
        put("images", buildJsonArray { m.images.forEach { add(JsonPrimitive(it.replace(base, "{BOX}"))) } })
    }

    /** The JSON body, or the raw text, with the box's address made stable. */
    private fun body(raw: String, base: String): JsonElement = try {
        Json.parseToJsonElement(raw.replace(base, "{BOX}"))
    } catch (_: Exception) {
        JsonPrimitive(raw)
    }

    /**
     * The /chat/stream request without what depends on this machine or the clock: the
     * phone's tailnet address, its location, and each message's timestamp.
     */
    private fun streamRequest(raw: String, base: String): JsonElement {
        val obj = body(raw, base) as? JsonObject ?: return JsonPrimitive(raw)
        val messages = (obj["messages"] as? kotlinx.serialization.json.JsonArray)?.map { m ->
            (m as? JsonObject)?.let { JsonObject(it.filterKeys { k -> k != "timestamp" }) } ?: m
        }
        return JsonObject(
            obj.filterKeys { it != "origin_device_id" && it != "location" } +
                (if (messages != null) mapOf("messages" to kotlinx.serialization.json.JsonArray(messages)) else emptyMap())
        )
    }

    @Test fun `every recorded stream leaves the turn it always has`() {
        val app = RuntimeEnvironment.getApplication()
        val store = BlackBoxStore(app)
        runBlocking {
            store.setOperator("golden-op")
            store.setProvider("anthropic")
            store.setModel("")
        }
        val vm = ChatViewModel(app)
        val base = server.url("").toString().trimEnd('/')
        vm.initialize(base)
        pumpUntil("the operator to resolve") { vm.operators.value == listOf("golden-op") }

        val mismatches = mutableListOf<String>()
        for (name in order) {
            val provider = resource("$name.provider").trim()
            runBlocking { store.setProvider(provider); store.setModel("") }
            val label = ChatProvider.fromId(provider).displayName
            pumpUntil("provider $provider") { vm.getProviderLabel() == label }

            streamBody = resource("$name.sse")
            requests.clear()
            val before = vm.messages.value.size
            vm.onInputChange(TextFieldValue("turn: $name"))
            vm.sendMessage()
            pumpUntil("$name to finish") {
                val s = vm.chatState.value
                s != ChatState.STREAMING && s != ChatState.THINKING &&
                    vm.messages.value.drop(before).none { it.isStreaming }
            }
            settle()

            val observed = buildJsonObject {
                put("fixture", name)
                put("provider", provider)
                put("messages", buildJsonArray { vm.messages.value.drop(before).forEach { add(msgJson(it, base)) } })
                put("chatState", vm.chatState.value.name)
                put("signalLabel", vm.signalLabel.value)
                put("cu", buildJsonObject {
                    put("status", vm.cuStatus.value)
                    put("step", vm.cuStep.value)
                    put("stepTotal", vm.cuStepTotal.value)
                    put("actionLabel", vm.cuActionLabel.value)
                    put("sessionId", vm.cuSessionId.value)
                    put("deviceId", vm.cuDeviceId.value)
                    put("screenshotUrl", vm.cuScreenshotUrl.value?.replace(base, "{BOX}")?.replace(Regex("\\?t=\\d+$"), "?t={T}"))
                })
                put("er", buildJsonObject {
                    put("status", vm.erStatus.value)
                    put("missionActive", vm.erMissionActive.value)
                    put("reasoning", vm.erReasoning.value)
                    put("camera", vm.erCamera.value)
                    put("cameraFrame", vm.erCameraFrame.value)
                })
                val stream = requests.firstOrNull { it.first == "/chat/stream" }
                put("streamRequest", if (stream != null) streamRequest(stream.second, base) else JsonNull)
                val save = requests.firstOrNull { it.first == "/chat/save" }
                put("save", if (save != null) body(save.second, base) else JsonNull)
                put("taskStatusPolls", buildJsonArray {
                    requests.filter { it.first.startsWith("/tasks/status/") }.map { it.first }.distinct().sorted()
                        .forEach { add(JsonPrimitive(it)) }
                })
            }

            val goldenText = javaClass.classLoader!!.getResource("sse/$name.golden.json")?.readText()
            if (goldenText == null) {
                val out = File("build/sse-golden").apply { mkdirs() }
                File(out, "$name.golden.json").writeText(json.encodeToString(JsonElement.serializer(), observed) + "\n")
                mismatches += "$name: no golden yet (written to ${File(out, "$name.golden.json").absolutePath})"
                continue
            }
            val golden = Json.parseToJsonElement(goldenText)
            if (golden != observed) {
                val out = File("build/sse-golden").apply { mkdirs() }
                File(out, "$name.observed.json").writeText(json.encodeToString(JsonElement.serializer(), observed) + "\n")
                mismatches += "$name differs from its golden:\n  golden:   $golden\n  observed: $observed"
            }
        }
        if (mismatches.isNotEmpty()) fail(mismatches.joinToString("\n\n"))
        assertEquals(order.size, order.toSet().size)
    }
}
