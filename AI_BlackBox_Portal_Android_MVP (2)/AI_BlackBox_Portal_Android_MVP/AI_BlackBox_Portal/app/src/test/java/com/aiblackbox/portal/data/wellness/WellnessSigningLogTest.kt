package com.aiblackbox.portal.data.wellness

import android.app.Application
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.api.SSEClient
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.runBlocking
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import okhttp3.Headers.Companion.headersOf
import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowLog
import java.util.concurrent.TimeUnit

/**
 * The log policy (rules W32 and "the key never leaves the interceptor"): a signed chat turn
 * and a Show numbers read, run through the app's real HTTP and SSE clients, leave neither the
 * device key, the signature, the header, the person's prompt nor the health numbers of the
 * reply or the ref in logcat.
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class)
class WellnessSigningLogTest {

    private val server = MockWebServer()
    private val op = WellnessFixtures.OPERATOR
    private val k = OriginSigner.encodeKey(ByteArray(32) { (it * 11 + 5).toByte() })
    private val facts by lazy {
        WellnessSigningInterceptor.Facts(
            WellnessBindingRecord(operator = op, enrollmentId = "en_0f1c0f1c", epoch = 1, k = k),
            WellnessSyncState(state = WellnessRules.EnrollmentState.ACTIVE.wire),
            server.url("").toString(),
        )
    }
    private val client = OkHttpClient.Builder().addInterceptor(WellnessSigningInterceptor({ facts })).build()

    @Before fun setUp() {
        ShadowLog.clear()
        server.start()
    }

    @After fun tearDown() = server.close()

    private fun logs(): String = ShadowLog.getLogs().joinToString("\n") { "${it.tag}: ${it.msg} ${it.throwable ?: ""}" }

    @Test
    fun `a signed chat turn and a ref read leave no key, signature, prompt or value in the log`() = runBlocking {
        val prompt = "How did I sleep last night? My resting rate felt high"
        server.enqueue(MockResponse.Builder().code(200).headers(headersOf("Content-Type", "text/event-stream"))
            .body("event: stream_start\ndata: {\"provider\": \"anthropic\", \"wellness_turn\": \"wt_0123456789abcdef\"}\n\n" +
                "event: wellness_used\ndata: {\"ref\": \"w-20261028-ab12\", \"categories\": [\"sleep\"]}\n\n" +
                "event: content\ndata: \"You slept 6 h 49 min, resting 57 bpm.\"\n\n" +
                "event: done\ndata: {\"content\": \"You slept 6 h 49 min, resting 57 bpm.\"}\n\n")
            .build())
        val api = BlackBoxApi(server.url("").toString().trimEnd('/'), client)
        val events = SSEClient(api).stream("/chat/stream",
            """{"messages":[{"role":"user","content":"$prompt"}],"operator":"$op"}""", signHealth = true).toList()
        assertTrue(events.any { it.event == "wellness_used" })
        val header = server.takeRequest(5, TimeUnit.SECONDS)!!.headers[OriginSigner.HEADER]!!
        val sig = header.substringAfter("sig=")

        server.enqueue(MockResponse.Builder().code(200).body(
            """{"ref":"w-20261028-ab12","ts":1,"tool":"wellness_summary","categories":["heart"],
               "payload":{"ok":true,"tool":"wellness_summary","metrics":[{"metric":"resting_hr","label":"Resting heart rate","display":"57 bpm"}]}}""",
        ).build())
        val result = WellnessRefClient.fetch(api, listOf("w-20261028-ab12"), op)
        assertTrue(result is ShowNumbersResult.Loaded)
        val refHeader = server.takeRequest(5, TimeUnit.SECONDS)!!.headers[OriginSigner.HEADER]!!

        val all = logs()
        assertTrue("the run logged something to check", all.isNotBlank())
        for (secret in listOf(k, sig, header, refHeader, refHeader.substringAfter("sig="))) {
            assertFalse("a log line carries a secret", all.contains(secret))
        }
        for (value in listOf("6 h 49 min", "57 bpm", prompt)) assertFalse("a log line carries \"$value\"", all.contains(value))
        assertEquals(0, ShadowLog.getLogs().count { it.msg.contains("X-BlackBox-Origin") })
    }
}
