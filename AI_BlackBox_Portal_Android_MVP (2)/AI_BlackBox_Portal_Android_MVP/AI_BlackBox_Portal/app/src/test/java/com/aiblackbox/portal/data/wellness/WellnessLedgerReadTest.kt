package com.aiblackbox.portal.data.wellness

import com.aiblackbox.portal.data.api.ApiHttpException
import com.aiblackbox.portal.data.api.BlackBoxApi
import kotlinx.coroutines.runBlocking
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import mockwebserver3.RecordedRequest
import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import java.util.concurrent.TimeUnit

/**
 * The Timeline's and the SNAP peek sheet's ledger read (contract (a)) over a real HTTP hop to a
 * stand-in box: signed for the enrolled operator on screen, asked once more unsigned when the box
 * refuses the proof (401 / 409 / 410), so the Timeline never shows less than before, and plain
 * for anyone else.
 */
class WellnessLedgerReadTest {

    private val server = MockWebServer()
    private val op = WellnessFixtures.OPERATOR
    private val k = OriginSigner.encodeKey(ByteArray(32) { (it * 7 + 3).toByte() })

    @Before fun setUp() = server.start()

    @After fun tearDown() = server.close()

    private val api by lazy {
        val facts = WellnessSigningInterceptor.Facts(
            WellnessBindingRecord(op, "en_0f1c0f1c", 1, k, boxOrigin = server.url("").toString()),
            WellnessSyncState(state = WellnessRules.EnrollmentState.ACTIVE.wire), server.url("").toString())
        val client = OkHttpClient.Builder().addInterceptor(WellnessSigningInterceptor({ facts }, { 1_793_000_000L })).build()
        BlackBoxApi(server.url("").toString().trimEnd('/'), client)
    }

    private fun answer(code: Int, body: String = "{}") = server.enqueue(MockResponse.Builder().code(code).body(body).build())

    private fun taken(): RecordedRequest = server.takeRequest(5, TimeUnit.SECONDS)!!

    private val masked = """{"snap_id":"SNAP-20261028-42","content":"WELLNESS: sleep · ref w-20261028-ab12 · values not recorded"}"""
    private val hidden = """{"snap_id":"SNAP-20261028-42","content":"WELLNESS: [wellness conversation · not shared]"}"""

    @Test
    fun `the enrolled operator's read is one signed request`() = runBlocking {
        answer(200, masked)
        assertEquals(masked, WellnessLedgerRead.fetch(api, "SNAP-20261028-42", op))
        val req = taken()
        assertEquals("/fossil/snapshot/SNAP-20261028-42", req.target)
        assertNotNull(req.headers[OriginSigner.HEADER])
        assertEquals(1, server.requestCount)
    }

    @Test
    fun `a refused proof is asked once more unsigned, and the unsigned answer is what shows`() = runBlocking {
        for (code in listOf(401, 409, 410)) {
            answer(code, """{"error":"bad_signature"}""")
            answer(200, hidden)
            assertEquals("$code", hidden, WellnessLedgerRead.fetch(api, "SNAP-20261028-42", op))
            assertNotNull("$code: the first ask is signed", taken().headers[OriginSigner.HEADER])
            val retry = taken()
            assertNull("$code: the retry is not", retry.headers[OriginSigner.HEADER])
            assertEquals("/fossil/snapshot/SNAP-20261028-42", retry.target)
        }
        assertEquals("exactly one retry each", 6, server.requestCount)
    }

    @Test
    fun `a refused retry, or any other failure, is the read's failure, never a loop`() = runBlocking {
        answer(401)
        answer(401)
        try {
            WellnessLedgerRead.fetch(api, "SNAP-20261028-42", op)
            fail("a second refusal is the answer")
        } catch (e: ApiHttpException) {
            assertEquals(401, e.code)
        }
        answer(404, """{"detail":"Snapshot SNAP-9 not found"}""")
        try {
            WellnessLedgerRead.fetch(api, "SNAP-9", op)
            fail("not found")
        } catch (e: ApiHttpException) {
            assertEquals(404, e.code)
        }
        assertEquals("no retry after a 404", 3, server.requestCount)
    }

    @Test
    fun `anyone else on screen, nobody, or an id off the list reads exactly as before`() = runBlocking {
        for ((id, screen) in listOf("SNAP-42" to "someone-else", "SNAP-42" to null, "SNAP-42" to " ", "snap-42" to op)) {
            answer(200, hidden)
            assertEquals(hidden, WellnessLedgerRead.fetch(api, id, screen))
            val req = taken()
            assertNull("$id for $screen", req.headers[OriginSigner.HEADER])
            assertEquals("/fossil/snapshot/$id", req.target)
        }
        assertTrue(server.requestCount == 4)
    }
}
