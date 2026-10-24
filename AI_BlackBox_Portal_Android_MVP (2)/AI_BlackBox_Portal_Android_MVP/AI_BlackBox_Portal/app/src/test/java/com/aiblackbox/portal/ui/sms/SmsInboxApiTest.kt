package com.aiblackbox.portal.ui.sms

import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.store.OperatorUnresolvedException
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import mockwebserver3.RecordedRequest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Before
import org.junit.Test
import java.util.concurrent.TimeUnit

/**
 * A14-ui-misc-07 — the SMS inbox acted as whichever operator was current when the
 * screen FIRST opened: switching operator on the SMS screen kept reading, polling and
 * texting as the previous one. [SmsInboxApi] reads the operator at EVERY request, so a
 * switch takes effect on the very next one. Real HTTP against a local MockWebServer.
 */
class SmsInboxApiTest {

    private val server = MockWebServer()
    private var operator = "Ana"
    private lateinit var sms: SmsInboxApi

    @Before fun setUp() {
        server.start()
        sms = SmsInboxApi(BlackBoxApi(server.url("").toString().removeSuffix("/"))) { operator }
    }

    @After fun tearDown() = server.close()

    private fun reply(body: String = "{}") = server.enqueue(MockResponse.Builder().body(body).build())
    private val SENT = """{"success":true,"error":null,"message_id":1}"""
    private fun taken(): RecordedRequest = server.takeRequest(5, TimeUnit.SECONDS)!!
    private fun bodyOf(r: RecordedRequest) = Json.parseToJsonElement(r.body!!.utf8()).jsonObject

    @Test fun `every request carries the operator current at that request`() = runBlocking {
        repeat(4) { reply() }
        sms.threads()
        assertEquals("Ana", taken().url.queryParameter("operator"))

        operator = "Ben"  // switched in the top bar while the inbox is open
        sms.threads()
        assertEquals("Ben", taken().url.queryParameter("operator"))
        sms.unread()
        assertEquals("Ben", taken().url.queryParameter("operator"))
        sms.preferences()
        assertEquals("Ben", taken().url.queryParameter("operator"))
    }

    @Test fun `a send goes out as the current operator, JSON-escaped`() = runBlocking {
        reply(SENT)
        sms.send(to = "+15550001111", message = "say \"hi\"\nnow", fromNumber = null)
        operator = "Ben Ng"
        reply(SENT)
        sms.send(to = "+15550002222", message = "x", fromNumber = "+15550009999")

        val first = bodyOf(taken())
        assertEquals("Ana", first["operator"]!!.jsonPrimitive.content)
        assertEquals("say \"hi\"\nnow", first["message"]!!.jsonPrimitive.content)
        assertNull(first["from_number"])
        val second = bodyOf(taken())
        assertEquals("Ben Ng", second["operator"]!!.jsonPrimitive.content)
        assertEquals("+15550009999", second["from_number"]!!.jsonPrimitive.content)
    }

    // A14-ui-misc-03: the body used to be string-built, escaping only quotes, so a newline
    // (the compose box is multi-line), a backslash or a tab made every send a 422.
    @Test fun `newlines, backslashes, tabs and quotes in a message stay text`() = runBlocking {
        reply("""{"success":true,"error":null,"message_id":7}""")
        val text = "line one\nC:\\Users\\pat\t\"tab\" \\o/ \u2603"
        sms.send(to = "+15550001111", message = text, fromNumber = "+1555000\"9999")
        val body = bodyOf(taken())
        assertEquals(text, body["message"]!!.jsonPrimitive.content)
        assertEquals("+1555000\"9999", body["from_number"]!!.jsonPrimitive.content)
    }

    // The box answers 200 with success=false when the gateway refused or is down; that
    // was reported as "Message sent" (and the draft was already gone).
    @Test fun `a send the box reports as failed throws with the box's reason`() = runBlocking {
        reply("""{"success":false,"error":"Not connected to AMI","message_id":12}""")
        val err = assertThrows(SmsSendFailedException::class.java) {
            runBlocking { sms.send(to = "+15550001111", message = "hi", fromNumber = null) }
        }
        assertEquals("Not connected to AMI", err.message)
        reply("""{"success":false}""")
        val bare = assertThrows(SmsSendFailedException::class.java) {
            runBlocking { sms.send(to = "+15550001111", message = "hi", fromNumber = null) }
        }
        assertEquals("Send failed", bare.message)
        reply("not json")
        assertThrows(SmsSendFailedException::class.java) {
            runBlocking { sms.send(to = "+15550001111", message = "hi", fromNumber = null) }
        }
        Unit
    }

    @Test fun `operator and phone are query-encoded`() = runBlocking {
        operator = "Ana Lee"
        reply()
        sms.messages("+15550001111")
        val r = taken()
        assertEquals("Ana Lee", r.url.queryParameter("operator"))
        assertEquals("+15550001111", r.url.queryParameter("phone"))
    }

    @Test fun `with no operator nothing is read and nothing is sent`() = runBlocking {
        operator = "  "
        assertNull(sms.threads())
        assertNull(sms.unread())
        assertNull(sms.preferences())
        assertNull(sms.messages("+15550001111"))
        assertThrows(OperatorUnresolvedException::class.java) {
            runBlocking { sms.send("+15550001111", "hi", null) }
        }
        assertThrows(OperatorUnresolvedException::class.java) {
            runBlocking { sms.savePreferences("anthropic", "") }
        }
        assertEquals(0, server.requestCount)
    }

    @Test fun `the model preference saves Auto as an empty model`() = runBlocking {
        reply()
        sms.savePreferences("openai", "")
        val body = bodyOf(taken())
        assertEquals("Ana", body["operator"]!!.jsonPrimitive.content)
        assertEquals("openai", body["sms_provider"]!!.jsonPrimitive.content)
        assertEquals("", body["sms_model"]!!.jsonPrimitive.content)
    }

    // ── the preference the inbox shows ───────────────────────────────────────

    @Test fun `no stored model is Auto, never a pinned id`() {
        assertEquals(SmsPreference("anthropic", ""), SmsPreference.parse("{}"))
        assertEquals(SmsPreference("anthropic", ""), SmsPreference.parse("not json"))
        assertEquals(SmsPreference("anthropic", ""), SmsPreference.parse("""{"sms_provider":"anthropic","sms_model":null}"""))
        assertEquals(SmsPreference("openai", "gpt-5"), SmsPreference.parse("""{"sms_provider":"openai","sms_model":"gpt-5"}"""))
        assertEquals("", SmsPreference.DEFAULT.model)
    }

    @Test fun `the app's gemini is saved as the Portal's google, so both surfaces show it`() = runBlocking {
        // The box treats the two alike, but the Portal's select only lists "google": a saved
        // "gemini" showed blank there, and a model edit then saved provider anthropic.
        reply()
        sms.savePreferences("gemini", "")
        assertEquals("google", bodyOf(taken())["sms_provider"]!!.jsonPrimitive.content)
        reply()
        sms.savePreferences("anthropic", "claude-opus-5-5")
        assertEquals("anthropic", bodyOf(taken())["sms_provider"]!!.jsonPrimitive.content)
        assertEquals("google", SmsPreference.wireProvider("gemini"))
        assertEquals("xai", SmsPreference.wireProvider("xai"))
    }

    @Test fun `the Portal's google provider id shows as gemini, the id this app's menu uses`() {
        assertEquals("gemini", SmsPreference.parse("""{"sms_provider":"google","sms_model":""}""").provider)
        assertEquals("gemini", SmsPreference.parse("""{"sms_provider":"gemini"}""").provider)
    }
}
