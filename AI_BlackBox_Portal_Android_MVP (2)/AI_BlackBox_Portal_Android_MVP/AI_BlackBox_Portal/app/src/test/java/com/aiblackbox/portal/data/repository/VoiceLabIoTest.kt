package com.aiblackbox.portal.data.repository

import com.aiblackbox.portal.data.api.BlackBoxApi
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import java.io.File
import java.util.concurrent.atomic.AtomicInteger
import kotlin.coroutines.CoroutineContext

/**
 * Audit rank 60 (A10-data-remote-01, -02):
 *  - the ElevenLabs and xAI clone/delete calls ran a blocking OkHttp execute() on the
 *    caller's (Main) thread, so on a device they threw NetworkOnMainThreadException and
 *    every clone/delete failed before a byte left the phone. They now run on the IO
 *    dispatcher (here a counting one, since the JVM has no StrictMode to trip).
 *  - status branching read "HTTP <code>" out of the message, which the backend's
 *    FastAPI `detail` replaced, so a 422/409 came back as code 0. It now reads
 *    ApiHttpException.code. Real HTTP against a local MockWebServer.
 */
class VoiceLabIoTest {

    private val server = MockWebServer()
    private lateinit var api: BlackBoxApi
    private lateinit var sample: File

    /** Dispatches to Dispatchers.IO and counts how often it was used. */
    private class CountingIo : CoroutineDispatcher() {
        val dispatches = AtomicInteger()
        override fun dispatch(context: CoroutineContext, block: Runnable) {
            dispatches.incrementAndGet()
            Dispatchers.IO.dispatch(context, block)
        }
    }

    @Before fun setUp() {
        server.start()
        api = BlackBoxApi(server.url("").toString().removeSuffix("/"))
        sample = File.createTempFile("voice-sample", ".wav").apply { writeBytes(ByteArray(64)) }
    }

    @After fun tearDown() {
        server.close()
        sample.delete()
    }

    private fun reply(code: Int, body: String) =
        server.enqueue(MockResponse.Builder().code(code).body(body).build())

    @Test fun `the four ElevenLabs and xAI clone and delete calls run on the IO dispatcher`() = runBlocking {
        val io = CountingIo()
        val repo = VoiceLabRepository(api, io)
        reply(200, """{"voice_id":"v1","requires_verification":false}""")
        assertEquals("v1", repo.cloneVoice("Me", listOf(sample), consent = true).voiceId)
        reply(200, """{"ok":true,"in_use":[]}""")
        assertTrue(repo.deleteVoice("v1").ok)
        reply(200, """{"voice_id":"x1"}""")
        assertEquals("x1", repo.cloneXaiVoice("Me", sample, consent = true))
        reply(200, """{"ok":true}""")
        assertTrue(repo.deleteXaiVoice("x1"))
        assertTrue("every blocking call was dispatched to IO (${io.dispatches.get()})", io.dispatches.get() >= 4)
    }

    @Test fun `a FastAPI detail error keeps its status code`() = runBlocking {
        val repo = VoiceLabRepository(api)
        reply(422, """{"detail":"Consent is required to clone a voice."}""")
        try {
            repo.designVoice("a calm narrator")
            fail("a 422 must surface")
        } catch (e: VoiceLabException) {
            assertEquals(422, e.status)
            assertEquals("Consent is required to clone a voice.", e.message)
        }
    }

    @Test fun `a clone refused by the backend surfaces its status`() = runBlocking {
        val repo = VoiceLabRepository(api)
        reply(400, """{"detail":"Sample too short"}""")
        try {
            repo.cloneVoice("Me", listOf(sample), consent = true)
            fail("a 400 must surface")
        } catch (e: VoiceLabException) {
            assertEquals(400, e.status)
        }
    }

    @Test fun `an embeddings migration already running (409 with detail) is not an error`() = runBlocking {
        val repo = UpdateRepository(api)
        reply(409, """{"detail":"An embeddings migration is already running."}""")
        repo.startEmbeddingsMigration("some-model")  // must not throw
        reply(500, """{"detail":"boom"}""")
        try {
            repo.startEmbeddingsMigration("some-model")
            fail("other failures still propagate")
        } catch (e: java.io.IOException) {
            assertEquals("boom", e.message)
        }
    }
}
