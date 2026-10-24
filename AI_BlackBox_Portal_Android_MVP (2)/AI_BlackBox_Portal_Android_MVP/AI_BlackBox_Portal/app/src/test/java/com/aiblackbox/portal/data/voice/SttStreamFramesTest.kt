package com.aiblackbox.portal.data.voice

import com.aiblackbox.portal.data.api.WsMessage
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.TimeUnit

/**
 * The /ws/stt frames on the native app (audit rank 52: W24, A08-cli-agent-ui-07,
 * O08-stt-realtime-04), driven from the SAME fixture the server and Portal suites read:
 * tests/fixtures/stt_ws_frames.json at the repository root.
 *
 * WHAT WAS BROKEN. parseMessage only emitted an Error for stt_error: capture kept streaming on
 * the open socket, and when the server closed it the reconnect loop opened a fresh leg to the
 * same provider (an exhausted ElevenLabs quota answered every reconnect with the same error)
 * while the CLI mic showed idle. stt_status frames (provider_override, loading_models) were
 * dropped. Now an stt_error is terminal (capture stops, no reconnect), carries the server's
 * code/provider/switchable, and stt_status drives [SttStreamClient.status].
 */
class SttStreamFramesTest {

    private val fixture: JsonObject = loadFixture()
    private val frames = fixture["frames"]!!.jsonArray.map { it.jsonObject }

    private val sockets = CopyOnWriteArrayList<FakeWebSocketClient>()
    private val records = CopyOnWriteArrayList<FakeAudioRecord>()
    private val events = CopyOnWriteArrayList<SttEvent>()
    private val collector = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    private val stt = SttStreamClient(
        OkHttpClient(),
        "ws://box.test",
        unprocessedSupported = false,
        wsFactory = { FakeWebSocketClient().also { sockets += it } },
        recordFactory = { _, sampleRate, _ -> FakeAudioRecord(sampleRate).also { records += it } },
    )

    init {
        collector.launch { stt.events.collect { events += it } }
    }

    @After
    fun tearDown() {
        stt.stop()
        sockets.forEach { it.incoming.trySend(WsMessage.Text(STT_DONE)) }
        records.forEach { rec -> repeat(3) { rec.feed(0) } }
        collector.cancel()
    }

    private fun frame(name: String): JsonObject =
        frames.first { it["name"]!!.jsonPrimitive.content == name }

    private fun JsonObject.str(key: String): String? =
        (this[key] as? JsonPrimitive)?.takeUnless { it is JsonNull }?.content

    private fun waitUntil(what: String, condition: () -> Boolean) {
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(FakeAudioRecord.WAIT_SECONDS)
        while (!condition()) {
            assertTrue("timed out waiting for $what", System.nanoTime() < deadline)
            Thread.sleep(5)
        }
    }

    /** Start, open socket leg 0, and return the recorder its capture loop is parked in. */
    private fun connected(): FakeAudioRecord {
        stt.start()
        waitUntil("socket leg 0") { sockets.isNotEmpty() }
        sockets[0].incoming.trySend(WsMessage.Connected)
        waitUntil("the recorder") { records.isNotEmpty() }
        val rec = records[0]
        assertTrue("the capture loop never reached read()", rec.awaitRead())
        return rec
    }

    // ---------------------------------------------------------------------------------------
    // Parsing: every fixture frame
    // ---------------------------------------------------------------------------------------

    @Test
    fun `every fixture frame parses to the event its expectation names`() {
        for (entry in frames) {
            val name = entry["name"]!!.jsonPrimitive.content
            val raw = entry["frame"]!!.jsonObject
            val expect = entry["expect"]!!.jsonObject
            val event = SttFrames.parse(raw)
            when (expect["kind"]!!.jsonPrimitive.content) {
                "error" -> {
                    val error = event as? SttEvent.Error ?: error("$name: not an Error: $event")
                    assertEquals(name, raw.str("message"), error.message)
                    assertEquals(name, expect.str("code"), error.code)
                    assertEquals(name, expect.str("provider"), error.provider)
                    assertEquals(name, expect["switchable"]!!.jsonPrimitive.booleanOrNull, error.switchable)
                }
                "status" -> {
                    val status = event as? SttEvent.Status ?: error("$name: not a Status: $event")
                    assertEquals(name, expect.str("state"), status.state)
                    assertEquals(name, expect.str("provider"), status.provider)
                    assertEquals(name, expect.str("overridden_from"), status.overriddenFrom)
                }
                "delta" -> assertEquals(name, SttEvent.Delta(expect.str("text")!!), event)
                "final" -> assertEquals(name, SttEvent.Final(expect.str("text")!!), event)
                "done" -> assertNull("$name: stt_done is a terminal marker, not an event", event)
                else -> error("$name: unknown kind")
            }
        }
    }

    @Test
    fun `warming follows the fixture`() {
        for (entry in frames.filter { it["expect"]!!.jsonObject.str("kind") == "status" }) {
            val name = entry["name"]!!.jsonPrimitive.content
            val status = SttFrames.parse(entry["frame"]!!.jsonObject) as SttEvent.Status
            val expectWarming = entry["expect"]!!.jsonObject["warming"]!!.jsonPrimitive.booleanOrNull!!
            // provider_override arrives as a session starts; from a warming session,
            // every other state but loading_models ends the warm-up.
            val before = if (status.state == SttFrames.STATE_PROVIDER_OVERRIDE) SttUiStatus()
                else SttUiStatus(warming = true)
            assertEquals(name, expectWarming, before.reduce(status).warming)
        }
    }

    @Test
    fun `provider_override renders a chip naming both providers by their catalog labels`() {
        val status = SttFrames.parse(frame("provider_override")["frame"]!!.jsonObject) as SttEvent.Status
        val ui = SttUiStatus().reduce(status)
        val labels = SttFrames.labels(fixture["catalog"]!!.jsonObject)
        val text = SttFrames.statusText(ui, labels)
        assertNotNull(text)
        assertTrue(text!!, text.contains(labels.getValue("openai")))
        assertTrue(text, text.contains(labels.getValue("elevenlabs")))
        assertNull("a clean session shows no chip", SttFrames.statusText(SttUiStatus(), labels))
        assertNotNull(SttFrames.statusText(SttUiStatus(warming = true), labels))
    }

    @Test
    fun `switch alternatives follow the fixture`() {
        val catalog = fixture["catalog"]!!.jsonObject
        for ((failed, ids) in fixture["switch_alternatives"]!!.jsonObject) {
            assertEquals(failed, ids.jsonArray.map { it.jsonPrimitive.content },
                SttFrames.switchAlternatives(catalog, failed).map { it.id })
        }
        assertEquals(emptyList<SttProviderChoice>(), SttFrames.switchAlternatives(null, "openai"))
    }

    // ---------------------------------------------------------------------------------------
    // The client: stt_error is terminal, stt_status drives the status flow
    // ---------------------------------------------------------------------------------------

    private fun assertErrorIsTerminal(name: String) {
        val entry = frame(name)
        val rec = connected()

        sockets[0].incoming.trySend(WsMessage.Text(entry["frame"]!!.jsonObject.toString()))

        waitUntil("the Error event") { events.any { it is SttEvent.Error } }
        val error = events.filterIsInstance<SttEvent.Error>().single()
        val expect = entry["expect"]!!.jsonObject
        assertEquals(expect.str("code"), error.code)
        assertEquals(expect["switchable"]!!.jsonPrimitive.booleanOrNull, error.switchable)
        waitUntil("the session to stop streaming") { !stt.isStreaming.value }

        // The in-flight read returns; the leg's loop must end, not stream on.
        rec.feed(1)
        assertTrue("capture kept running after an stt_error", rec.awaitRelease())

        // The server ends the session (stt_done, then the close). No new leg may open.
        sockets[0].incoming.trySend(WsMessage.Text(STT_DONE))
        sockets[0].incoming.close()
        Thread.sleep(SETTLE_MS * 3)
        assertEquals("an stt_error must not be answered with a reconnect", 1, sockets.size)
        assertEquals("no second recorder after an stt_error", 1, records.size)
    }

    @Test
    fun `quota_exceeded stops capture and does not reconnect`() =
        assertErrorIsTerminal("elevenlabs_quota_exceeded")

    @Test
    fun `a non-switchable error is terminal too`() =
        assertErrorIsTerminal("no_provider")

    @Test
    fun `an error from an older server without a code is terminal too`() =
        assertErrorIsTerminal("legacy_error_without_code")

    @Test
    fun `stt_status frames drive the status flow and a new session starts clean`() {
        connected()
        val ws = sockets[0]

        ws.incoming.trySend(WsMessage.Text(frame("loading_models")["frame"]!!.jsonObject.toString()))
        waitUntil("warming") { stt.status.value.warming }
        ws.incoming.trySend(WsMessage.Text(frame("listening")["frame"]!!.jsonObject.toString()))
        waitUntil("warm") { !stt.status.value.warming }
        ws.incoming.trySend(WsMessage.Text(frame("provider_override")["frame"]!!.jsonObject.toString()))
        waitUntil("the override") { stt.status.value.overrideProvider == "openai" }
        assertEquals("elevenlabs", stt.status.value.overriddenFrom)
        assertTrue(events.any { it is SttEvent.Status })

        stt.stop()
        records[0].feed(1)
        ws.incoming.trySend(WsMessage.Text(STT_DONE))
        waitUntil("the session to end") { events.any { it is SttEvent.SessionEnded } }
        assertEquals("the status goes with the session", SttUiStatus(), stt.status.value)
    }

    @Test
    fun `start sends the provider it was given`() {
        stt.start(provider = "openai")
        waitUntil("socket leg 0") { sockets.isNotEmpty() }
        sockets[0].incoming.trySend(WsMessage.Connected)
        waitUntil("stt_start") { sockets[0].sent.isNotEmpty() }
        val start = Json.parseToJsonElement(sockets[0].sent.first()).jsonObject
        assertEquals("stt_start", start.str("type"))
        assertEquals("openai", start.str("provider"))
        assertFalse(start.str("provider").isNullOrEmpty())
    }

    private companion object {
        const val SETTLE_MS = 100L
        const val STT_DONE = """{"type":"stt_done"}"""
        const val FIXTURE = "tests/fixtures/stt_ws_frames.json"

        /** The repository's shared fixture, found by walking up from the module directory. */
        fun loadFixture(): JsonObject {
            var dir: File? = File(System.getProperty("user.dir")).absoluteFile
            while (dir != null) {
                val candidate = File(dir, FIXTURE)
                if (candidate.isFile) return Json.parseToJsonElement(candidate.readText()).jsonObject
                dir = dir.parentFile
            }
            error("shared fixture $FIXTURE not found above ${System.getProperty("user.dir")}")
        }
    }
}
