package com.aiblackbox.portal.ui.chat

import android.app.Application
import android.content.Intent
import android.os.Looper
import com.aiblackbox.portal.BackgroundTaskService
import com.aiblackbox.portal.data.api.BlackBoxApi
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import mockwebserver3.Dispatcher
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import mockwebserver3.RecordedRequest
import okio.Buffer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowToast
import java.io.File
import java.util.Collections

/**
 * Chat's spoken replies, out of NativeMainActivity (rank 76), against a fake box: the bubble
 * speaker, auto-TTS and plain speech each read the operator's voice, take /tts/batch for a
 * cloud voice and the async queue for an on-box one (with the /tts/batch fallback on a box
 * that has no queue), and leave the bubble in the right state with the toast it showed.
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class)
class ChatSpeechTest {

    private val app: Application = RuntimeEnvironment.getApplication()
    private lateinit var server: MockWebServer
    private val requests = Collections.synchronizedList(mutableListOf<Pair<String, String>>())
    private val routes = mutableMapOf<String, () -> MockResponse>()
    private val scope = CoroutineScope(Dispatchers.Main + SupervisorJob())

    private val events = Collections.synchronizedList(mutableListOf<String>())
    private val played = mutableListOf<File>()
    private var voice = "openai:alloy"
    private var operator = "op-a"
    private var api: BlackBoxApi? = null

    private val bubbles = object : ChatSpeech.Bubbles {
        override fun setGenerating(messageId: String, generating: Boolean, status: String?) {
            events += "generating($messageId, $generating, $status)"
        }
        override fun setAudioPath(messageId: String, path: String) {
            events += "audio($messageId, ${File(path).name.replace(Regex("\\d{10,}"), "T")})"
            audioFiles += File(path)
        }
        override fun usedHealth(messageId: String): Boolean = messageId in healthReplies
    }
    /** Bubbles whose turn used the person's health data (M7). */
    private val healthReplies = mutableSetOf<String>()
    private val audioFiles = mutableListOf<File>()

    private val speech by lazy {
        ChatSpeech(
            context = app,
            scope = scope,
            api = { api },
            operatorOf = { operator },
            voiceFor = { op -> if (op == operator) voice else "" },
            bubbles = bubbles,
            play = { played += it },
        )
    }

    private fun ok(body: String) = MockResponse.Builder().code(200).body(body).build()
    private fun bytes(content: ByteArray) = MockResponse.Builder().code(200).body(Buffer().write(content)).build()
    private fun status(code: Int) = MockResponse.Builder().code(code).body("{}").build()

    @Before fun setUp() {
        server = MockWebServer()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                val path = request.target.substringBefore('?')
                requests += path to (request.body?.utf8() ?: "")
                return routes[path]?.invoke() ?: status(404)
            }
        }
        server.start()
        api = BlackBoxApi(server.url("").toString().trimEnd('/'))
    }

    @After fun tearDown() {
        scope.cancel()
        server.close()
    }

    /** Pump the main looper until [done] (the code under test hops between IO and Main). */
    private fun awaitUntil(what: String, done: () -> Boolean) {
        val deadline = System.currentTimeMillis() + 10_000
        while (System.currentTimeMillis() < deadline) {
            shadowOf(Looper.getMainLooper()).idle()
            if (done()) return
            Thread.sleep(10)
        }
        fail("timed out waiting for $what; events=$events requests=$requests")
    }

    private fun batchBody(): Map<String, String> {
        val body = requests.single { it.first == "/tts/batch" }.second
        return Json.parseToJsonElement(body).jsonObject.mapValues { it.value.jsonPrimitive.content }
    }

    private fun serviceActions(): List<String?> {
        val shadow = shadowOf(app)
        val actions = mutableListOf<String?>()
        while (true) {
            val next: Intent = shadow.nextStartedService ?: break
            assertEquals(BackgroundTaskService::class.java.name, next.component?.className)
            actions += next.action
        }
        return actions
    }

    // ---- the bubble speaker ----

    @Test fun `the speaker turns a cloud voice into the bubble's clip through tts batch`() {
        routes["/tts/batch"] = { bytes("MP3DATA".toByteArray()) }
        speech.speakForMessage("msg12345-abc", "Hello **there**.")
        assertEquals("Generating speech...", ShadowToast.getTextOfLatestToast())
        awaitUntil("the clip") { events.any { it.startsWith("audio(") } }

        assertEquals(
            listOf("generating(msg12345-abc, true, null)", "audio(msg12345-abc, tts_msg12345_T.mp3)"),
            events,
        )
        assertEquals("MP3DATA", audioFiles.single().readText())
        val sent = batchBody()
        assertEquals("openai", sent["provider"])
        assertEquals("alloy", sent["voice"])
        assertEquals("mp3", sent["format"])
        assertEquals("op-a", sent["operator"])
        // The keep-alive service runs for the whole generation.
        val actions = mutableListOf<String?>()
        awaitUntil("the keep-alive stop") { actions += serviceActions(); BackgroundTaskService.ACTION_STOP in actions }
        assertEquals(listOf(BackgroundTaskService.ACTION_START, BackgroundTaskService.ACTION_STOP), actions)
    }

    @Test fun `a gemini voice's clip is a wav`() {
        voice = "gemini-pro:Charon"
        routes["/tts/batch"] = { bytes(byteArrayOf(1, 2, 3)) }
        speech.speakForMessage("abcdefghij", "Hi.")
        awaitUntil("the clip") { events.any { it.startsWith("audio(") } }
        assertEquals("audio(abcdefghij, tts_abcdefgh_T.wav)", events.last())
    }

    @Test fun `a failed request resets the speaker and says why`() {
        routes["/tts/batch"] = { status(500) }
        speech.speakForMessage("m1", "Hi.")
        awaitUntil("the reset") { events.size == 2 }
        assertEquals(listOf("generating(m1, true, null)", "generating(m1, false, null)"), events)
        assertEquals("TTS failed: 500", ShadowToast.getTextOfLatestToast())
        val actions = mutableListOf<String?>()
        awaitUntil("the keep-alive stop") { actions += serviceActions(); BackgroundTaskService.ACTION_STOP in actions }
    }

    @Test fun `an empty clip resets the speaker`() {
        routes["/tts/batch"] = { ok("") }
        speech.speakForMessage("m1", "Hi.")
        awaitUntil("the reset") { events.size == 2 }
        assertEquals("generating(m1, false, null)", events.last())
        assertEquals("TTS: empty response", ShadowToast.getTextOfLatestToast())
    }

    @Test fun `no box yet resets the speaker without a request`() {
        api = null
        speech.speakForMessage("m1", "Hi.")
        awaitUntil("the reset") { events.size == 2 }
        assertEquals(listOf("generating(m1, true, null)", "generating(m1, false, null)"), events)
        assertTrue(requests.isEmpty())
    }

    @Test fun `an on-box voice on a box without the queue falls back to tts batch`() {
        voice = "qwen:Vivian"
        routes["/tts/batch"] = { bytes("WAVISH".toByteArray()) }
        speech.speakForMessage("m2", "Hi.")
        awaitUntil("the clip") { events.any { it.startsWith("audio(") } }
        assertEquals(
            listOf(
                "generating(m2, true, null)",
                "generating(m2, true, Queueing audio…)",
                // No queue (404): the chip clears, the spinner stays for the direct call.
                "generating(m2, true, null)",
                "audio(m2, tts_m2_T.mp3)",
            ),
            events,
        )
        assertEquals(listOf("/tts/queue", "/tts/batch"), requests.map { it.first })
        assertEquals("qwen", batchBody()["provider"])
    }

    @Test fun `an on-box voice goes through the queue and its finished clip`() {
        voice = "qwen:Vivian"
        routes["/tts/queue"] = { ok("""{"task_id":"t1"}""") }
        routes["/tts/task/t1"] = { ok("""{"status":"done","audio_url":"/ui/uploads/t1.wav"}""") }
        routes["/ui/uploads/t1.wav"] = { bytes("RIFF".toByteArray()) }
        speech.speakForMessage("m3", "Hi.")
        awaitUntil("the clip") { events.any { it.startsWith("audio(") } }
        assertEquals("audio(m3, tts_q_m3_T.wav)", events.last())
        assertEquals("RIFF", audioFiles.single().readText())
        assertEquals(listOf("/tts/queue", "/tts/task/t1", "/ui/uploads/t1.wav"), requests.map { it.first })
        val queued = Json.parseToJsonElement(requests.first().second).jsonObject
        assertEquals("qwen:Vivian", queued["voice"]!!.jsonPrimitive.content)
        assertEquals("op-a", queued["operator"]!!.jsonPrimitive.content)
    }

    @Test fun `a queue job that fails for good says so, without the retry hint or a second request`() {
        voice = "qwen:Vivian"
        routes["/tts/queue"] = { ok("""{"task_id":"t2"}""") }
        routes["/tts/task/t2"] = { ok("""{"status":"failed","error":"voice not found","retryable":false}""") }
        speech.speakForMessage("m4", "Hi.")
        awaitUntil("the reset") { events.lastOrNull() == "generating(m4, false, null)" }
        assertEquals("Audio failed: voice not found", ShadowToast.getTextOfLatestToast())
        assertTrue(requests.none { it.first == "/tts/batch" })
    }

    // ---- auto-TTS ----

    @Test fun `auto-TTS speaks a finished reply into its bubble`() {
        routes["/tts/batch"] = { bytes("MP3".toByteArray()) }
        var done = false
        scope.launch { speech.autoSpeak("r1", "The answer."); done = true }
        awaitUntil("auto-TTS") { done }
        assertEquals(listOf("generating(r1, true, null)", "audio(r1, auto_tts_T.mp3)"), events)
        assertEquals("op-a", batchBody()["operator"])
    }

    @Test fun `auto-TTS with no box resets the bubble`() {
        api = null
        var done = false
        scope.launch { speech.autoSpeak("r1", "The answer."); done = true }
        awaitUntil("auto-TTS") { done }
        assertEquals(listOf("generating(r1, true, null)", "generating(r1, false, null)"), events)
    }

    @Test fun `auto-TTS reads the operator current when it runs`() {
        routes["/tts/batch"] = { bytes("MP3".toByteArray()) }
        operator = "op-b"
        voice = "openai:nova"
        var done = false
        scope.launch { speech.autoSpeak("r1", "The answer."); done = true }
        awaitUntil("auto-TTS") { done }
        assertEquals("op-b", batchBody()["operator"])
        assertEquals("nova", batchBody()["voice"])
    }

    // ---- a reply that used health data (M7) ----

    @Test fun `auto-TTS never sends a health reply to a cloud voice`() {
        healthReplies += "h1"
        routes["/tts/batch"] = { bytes("MP3".toByteArray()) }
        for (v in listOf("openai:alloy", "gemini-pro:Charon", "elevenlabs:abc123", "local:af_heart", "")) {
            voice = v
            events.clear()
            var done = false
            scope.launch { speech.autoSpeak("h1", "You slept 6 h 49 min, resting 57 bpm."); done = true }
            awaitUntil("auto-TTS ($v)") { done }
            assertEquals(v, listOf("generating(h1, true, null)", "generating(h1, false, null)"), events)
        }
        assertTrue("no request left the phone: $requests", requests.isEmpty())
    }

    @Test fun `the speaker refuses a cloud voice for a health reply and says why`() {
        healthReplies += "h2"
        routes["/tts/batch"] = { bytes("MP3".toByteArray()) }
        speech.speakForMessage("h2", "You slept 6 h 49 min.")
        awaitUntil("the refusal") { events.size == 2 }
        assertEquals(listOf("generating(h2, true, null)", "generating(h2, false, null)"), events)
        assertEquals(com.aiblackbox.portal.data.wellness.WellnessChatRules.NOT_SPOKEN_MESSAGE, ShadowToast.getTextOfLatestToast())
        assertTrue(requests.isEmpty())
        val actions = mutableListOf<String?>()
        awaitUntil("the keep-alive stop") { actions += serviceActions(); BackgroundTaskService.ACTION_STOP in actions }
    }

    @Test fun `the on-box voice speaks a health reply, and other replies are unchanged`() {
        healthReplies += "h3"
        voice = "qwen:Vivian"
        routes["/tts/queue"] = { ok("""{"task_id":"t9"}""") }
        routes["/tts/task/t9"] = { ok("""{"status":"done","audio_url":"/ui/uploads/t9.wav"}""") }
        routes["/ui/uploads/t9.wav"] = { bytes("RIFF".toByteArray()) }
        speech.speakForMessage("h3", "You slept a little less than usual.")
        awaitUntil("the clip") { events.any { it.startsWith("audio(") } }
        assertEquals("audio(h3, tts_q_h3_T.wav)", events.last())
        assertTrue(requests.none { it.first == "/tts/batch" })

        voice = "openai:alloy"
        routes["/tts/batch"] = { bytes("MP3".toByteArray()) }
        events.clear()
        var done = false
        scope.launch { speech.autoSpeak("plain", "The answer."); done = true }
        awaitUntil("auto-TTS") { done }
        assertEquals(listOf("generating(plain, true, null)", "audio(plain, auto_tts_T.mp3)"), events)
    }

    @Test fun `only the on-box voice may speak a health reply`() {
        val rules = com.aiblackbox.portal.data.wellness.WellnessChatRules
        assertTrue(rules.maySpeak(true, "qwen"))
        assertTrue(rules.maySpeak(true, " QWEN "))
        for (p in listOf("openai", "gemini-pro", "gemini-flash", "elevenlabs", "local", "", null)) assertTrue(!rules.maySpeak(true, p))
        for (p in listOf("openai", "elevenlabs", "", null)) assertTrue(rules.maySpeak(false, p))
    }

    // ---- plain speech ----

    @Test fun `plain speech plays the clip once with no bubble`() {
        routes["/tts/batch"] = { bytes("MP3".toByteArray()) }
        speech.speak("Read this.")
        awaitUntil("playback") { played.isNotEmpty() }
        assertTrue(played.single().name.matches(Regex("tts_\\d+\\.mp3")))
        assertEquals("MP3", played.single().readText())
        assertTrue(events.isEmpty())
    }

    @Test fun `plain speech of an on-box voice plays the queued clip`() {
        voice = "qwen:Vivian"
        routes["/tts/queue"] = { ok("""{"task_id":"t3"}""") }
        routes["/tts/task/t3"] = { ok("""{"status":"done","audio_url":"/ui/uploads/t3.wav"}""") }
        routes["/ui/uploads/t3.wav"] = { bytes("RIFF".toByteArray()) }
        speech.speak("Read this.")
        awaitUntil("playback") { played.isNotEmpty() }
        assertTrue(played.single().name.matches(Regex("tts_\\d+\\.wav")))
        assertTrue(requests.none { it.first == "/tts/batch" })
    }

    @Test fun `plain speech that fails plays nothing`() {
        routes["/tts/batch"] = { status(500) }
        speech.speak("Read this.")
        awaitUntil("the request") { requests.isNotEmpty() }
        repeat(20) { shadowOf(Looper.getMainLooper()).idle(); Thread.sleep(5) }
        assertTrue(played.isEmpty())
    }
}
