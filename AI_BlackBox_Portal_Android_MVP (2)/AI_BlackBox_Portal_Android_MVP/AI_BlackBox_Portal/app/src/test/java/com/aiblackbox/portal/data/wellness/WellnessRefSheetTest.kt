package com.aiblackbox.portal.data.wellness

import com.aiblackbox.portal.data.api.BlackBoxApi
import kotlinx.coroutines.runBlocking
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.TimeUnit

/**
 * Show numbers (spec §6.1 "Chat"): the private sheet is built from the delivered envelope the
 * box keeps (`GET /wellness/ref/{ref}` = {ref, ts, tool, categories, payload}, payload being
 * the wellness_* envelope of Orchestrator/wellness/api.py), through its own display strings;
 * a refusal of any kind is "Not available". The envelopes are the synthetic fixture's
 * (operator wellness-fixture), as the box's tools return them.
 */
class WellnessRefSheetTest {

    private fun ref(tool: String, payload: String, ref: String = "w-20261028-ab12") =
        """{"ref":"$ref","ts":1793000000000,"tool":"$tool","categories":["sleep","heart"],"payload":$payload}"""

    private val summary = ref("wellness_summary", """
        {"ok":true,"tool":"wellness_summary","ref":"w-20261028-ab12","as_of":"2026-10-28T11:55-04:00","stale":false,
         "period":{"label":"last night","name":"last_night","start":"2026-10-27T12:00-04:00","end":"2026-10-28T12:00-04:00","tz":"UTC-04:00"},
         "metrics":[
          {"metric":"sleep_duration","category":"sleep","label":"Sleep","value":409.0,"unit":"min","display":"6 h 49 min",
           "spoken":"about seven hours","usual":{"median":438.0,"low":402.4,"high":473.6,"n":25},
           "vs_usual":{"label":"within your usual range","delta_display":"about 29 min less"},
           "coverage":{"label":"worn all night","n_used":84,"n_expected":85,"pct":99},"method":"sum of asleep stages","flags":[]},
          {"metric":"sleeping_hr_median","category":"heart","label":"Sleeping heart rate","value":51.9,"unit":"bpm","display":"52 bpm",
           "vs_usual":{"label":"within your usual range","delta_display":"about the same as usual"},
           "coverage":{"label":"worn all night","pct":99}},
          {"metric":"resting_hr","category":"heart","label":"Resting heart rate","value":null,"unit":"bpm","display":"not recorded",
           "vs_usual":null,"coverage":{"label":"reported","pct":null}}],
         "gaps":[{"metric":"hrv","reason":"not recorded by your watch"}],"troubles":[],
         "notes":["Sleep stages are device estimates; one night is noisy."]}
    """.trimIndent())

    @Test
    fun `a summary lists each metric's display string with its comparison, coverage, gaps and notes`() {
        val sheet = WellnessRefSheet.parse(summary)!!
        assertEquals("w-20261028-ab12", sheet.ref)
        assertEquals("Last night", sheet.title)
        assertEquals("2026-10-28T11:55-04:00", sheet.asOf)
        assertEquals(false, sheet.stale)
        assertEquals(
            listOf(
                WellnessRefSheet.Row("Sleep", "6 h 49 min", "within your usual range, about 29 min less"),
                WellnessRefSheet.Row("Sleeping heart rate", "52 bpm", "within your usual range, about the same as usual"),
                WellnessRefSheet.Row("Resting heart rate", "not recorded", null),
            ),
            sheet.rows,
        )
        assertEquals(listOf("Sleep: worn all night (99%)", "Sleeping heart rate: worn all night (99%)",
            "Resting heart rate: reported"), sheet.coverage)
        assertEquals(listOf("Not recorded: Heart rate variability (not recorded by your watch)",
            "Sleep stages are device estimates; one night is noisy."), sheet.notes)
    }

    @Test
    fun `a series lists each point's display string, a session list each session`() {
        val series = WellnessRefSheet.parse(ref("wellness_series", """
            {"ok":true,"tool":"wellness_series","metric":"steps","window":"the last 7 days","as_of":"2026-10-28T11:55-04:00",
             "points":[{"t":"2026-10-24","value":9072.0,"display":"9,072 steps","coverage_pct":100},
                       {"t":"2026-10-25","value":11204.0,"display":"11,204 steps","coverage_pct":80},
                       {"t":"2026-10-26","value":null,"display":null,"coverage_pct":0}]}
        """.trimIndent()))!!
        assertEquals("Steps, the last 7 days", series.title)
        assertEquals(listOf(
            WellnessRefSheet.Row("2026-10-24", "9,072 steps"),
            WellnessRefSheet.Row("2026-10-25", "11,204 steps", "coverage 80%"),
            WellnessRefSheet.Row("2026-10-26", "not recorded", "coverage 0%"),
        ), series.rows)

        val sessions = WellnessRefSheet.parse(ref("wellness_sessions", """
            {"ok":true,"tool":"wellness_sessions","kind":"sleep","window":"the last 7 days","as_of":"2026-10-28T11:55-04:00",
             "sessions":[{"session_ref":"s-20261028-eed26939","kind":"sleep","type":"sleep","start":"2026-10-27T23:31-04:00",
              "end":"2026-10-28T06:33-04:00","duration":"7 h 02 min","avg_hr":52.1,"avg_hr_display":"52 bpm","hr_coverage_pct":100,
              "source":"watch via Samsung Health"}]}
        """.trimIndent()))!!
        assertEquals("Sleep sessions, the last 7 days", sessions.title)
        assertEquals(listOf(WellnessRefSheet.Row("Sleep, 2026-10-27 23:31", "7 h 02 min",
            "average heart rate 52 bpm · watch via Samsung Health")), sessions.rows)
    }

    @Test
    fun `the history table shows each recorded cell with its unit and leaves gaps out`() {
        val sheet = WellnessRefSheet.parse(ref("wellness_history", """
            {"ok":true,"tool":"wellness_history","start":"2026-10-22","end":"2026-10-23","as_of":"2026-10-28T11:55-04:00",
             "columns":["day","sleep_duration","sleep_onset","sleeping_hr_median","steps","distance","night_worn","day_wear_h"],
             "units":{"sleep_duration":"min","sleep_onset":"clock","sleeping_hr_median":"bpm","steps":"steps","distance":"m"},
             "rows":[["2026-10-22",null,null,null,7213.0,5337.7,null,16.0],
                     ["2026-10-23",407.0,"23:34",52.1,9862.0,7298.0,1,16.0]]}
        """.trimIndent()))!!
        assertEquals("Day by day, 2026-10-22 to 2026-10-23", sheet.title)
        assertEquals(listOf(
            WellnessRefSheet.Row("2026-10-22", "Steps 7,213 steps · Distance 5.3 km"),
            WellnessRefSheet.Row("2026-10-23", "Sleep 6 h 47 min · Asleep 23:34 · Sleeping heart rate 52.1 bpm · " +
                "Steps 9,862 steps · Distance 7.3 km"),
        ), sheet.rows)
    }

    @Test
    fun `an envelope with no numbers, or no envelope at all, says so`() {
        val sources = WellnessRefSheet.parse(ref("wellness_sources", """{"ok":true,"tool":"wellness_sources","as_of":null}"""))!!
        assertEquals("Sources", sources.title)
        assertTrue(sources.rows.isEmpty())
        assertEquals(listOf("This result had no health numbers."), sources.notes)
        val refused = WellnessRefSheet.parse(ref("wellness_summary", """{"ok":false,"tool":"wellness_summary","error":"category_off"}"""))!!
        assertTrue(refused.rows.isEmpty())
        assertNull(WellnessRefSheet.parse("""{"error":"unknown_ref"}"""))
        assertNull(WellnessRefSheet.parse("not json"))
        assertNull(WellnessRefSheet.parse("""{"ref":"w-20261028-ab12","payload":"text"}"""))
    }

    @Test
    fun `a refused, unknown or unanswered ref is Not available, with no detail`() {
        assertEquals(ShowNumbersResult.NotAvailable, ShowNumbersResult.of(listOf(403 to """{"error":"not_allowed"}""")))
        assertEquals(ShowNumbersResult.NotAvailable, ShowNumbersResult.of(listOf(404 to """{"error":"unknown_ref"}""")))
        assertEquals(ShowNumbersResult.NotAvailable, ShowNumbersResult.of(listOf(401 to "", -1 to "")))
        assertEquals(ShowNumbersResult.NotAvailable, ShowNumbersResult.of(emptyList()))
        val some = ShowNumbersResult.of(listOf(404 to "", 200 to summary))
        assertTrue(some is ShowNumbersResult.Loaded)
        assertEquals(1, (some as ShowNumbersResult.Loaded).sheets.size)
    }

    // ── the fetch, over HTTP, signed ───────────────────────────────────────────────────────

    @Test
    fun `the fetch is a signed GET per ref naming the operator, and a 404 is Not available`() = runBlocking {
        val server = MockWebServer()
        server.start()
        try {
            val k = OriginSigner.encodeKey(ByteArray(32) { 9 })
            val facts = WellnessSigningInterceptor.Facts(
                WellnessBindingRecord(WellnessFixtures.OPERATOR, "en_0f1c0f1c", 1, k),
                WellnessSyncState(state = WellnessRules.EnrollmentState.ACTIVE.wire))
            val client = OkHttpClient.Builder().addInterceptor(WellnessSigningInterceptor({ facts })).build()
            val api = BlackBoxApi(server.url("").toString().trimEnd('/'), client)

            server.enqueue(MockResponse.Builder().code(200).body(summary).build())
            val ok = WellnessRefClient.fetch(api, listOf("w-20261028-ab12"), WellnessFixtures.OPERATOR)
            assertTrue(ok is ShowNumbersResult.Loaded)
            val req = server.takeRequest(5, TimeUnit.SECONDS)!!
            assertEquals("GET", req.method)
            assertEquals("/wellness/ref/w-20261028-ab12", req.url.encodedPath)
            assertEquals(WellnessFixtures.OPERATOR, req.url.queryParameter("operator"))
            assertNotNull(req.headers[OriginSigner.HEADER])

            server.enqueue(MockResponse.Builder().code(404).body("""{"error":"unknown_ref"}""").build())
            assertEquals(ShowNumbersResult.NotAvailable, WellnessRefClient.fetch(api, listOf("w-20261028-ffff"), WellnessFixtures.OPERATOR))
            server.enqueue(MockResponse.Builder().code(403).body("""{"error":"not_allowed"}""").build())
            assertEquals(ShowNumbersResult.NotAvailable, WellnessRefClient.fetch(api, listOf("w-20261028-ffff"), WellnessFixtures.OPERATOR))
            // a malformed ref never leaves the phone
            assertEquals(ShowNumbersResult.NotAvailable, WellnessRefClient.fetch(api, listOf("../wellness/erase"), WellnessFixtures.OPERATOR))
            assertEquals(3, server.requestCount)
        } finally {
            server.close()
        }
    }
}
