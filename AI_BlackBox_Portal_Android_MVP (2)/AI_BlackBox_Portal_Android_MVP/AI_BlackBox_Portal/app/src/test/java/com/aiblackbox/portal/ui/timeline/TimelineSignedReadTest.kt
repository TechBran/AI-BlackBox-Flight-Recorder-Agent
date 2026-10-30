package com.aiblackbox.portal.ui.timeline

import android.app.Application
import com.aiblackbox.portal.data.wellness.OriginSigner
import com.aiblackbox.portal.data.wellness.WellnessBindingRecord
import com.aiblackbox.portal.data.wellness.WellnessChatRules
import com.aiblackbox.portal.data.wellness.WellnessChatSigning
import com.aiblackbox.portal.data.wellness.WellnessFixtures
import com.aiblackbox.portal.data.wellness.WellnessRules
import com.aiblackbox.portal.data.wellness.WellnessSigningInterceptor
import com.aiblackbox.portal.data.wellness.WellnessSyncState
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import mockwebserver3.Dispatcher
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import mockwebserver3.RecordedRequest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import java.util.Collections

/**
 * The Timeline and the SNAP peek sheet fetch a snapshot through the signed ledger read
 * (contract (a)): on the enrolled phone, with its own operator on screen, the box answers with
 * the turn as minted (masked, its WELLNESS gauge intact), so Show numbers is finally offered on a
 * reloaded health turn; a refused proof is asked once more unsigned; another operator on screen
 * reads exactly as before. The stand-in box answers like the real one (snapshots its own
 * perform_mint wrote, resources `wellness/ledger_snapshots.json`).
 */
@OptIn(ExperimentalCoroutinesApi::class)
class TimelineSignedReadTest {

    private val server = MockWebServer()
    private val op = WellnessFixtures.OPERATOR
    private val requests = Collections.synchronizedList(mutableListOf<RecordedRequest>())
    private var refuseProof = 0

    private fun minted(key: String): String {
        val url = javaClass.classLoader!!.getResource("wellness/ledger_snapshots.json")!!
        return ((Json.parseToJsonElement(url.readText()) as JsonObject).getValue(key) as JsonPrimitive).content
    }

    private fun answer(content: String) = buildJsonObject {
        put("snap_id", JsonPrimitive("SNAP-20261030-001"))
        put("content", JsonPrimitive(content))
        put("metadata", buildJsonObject {
            put("operator", JsonPrimitive(op))
            put("timestamp", JsonPrimitive("2026-10-30T12:23:10Z"))
            put("type", JsonPrimitive("snapshot"))
        })
    }.toString()

    @Before fun setUp() {
        Dispatchers.setMain(UnconfinedTestDispatcher())
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                requests += request
                val signed = request.headers[OriginSigner.HEADER] != null
                if (signed && refuseProof > 0) {
                    refuseProof--
                    return MockResponse.Builder().code(401).body("""{"error":"bad_signature"}""").build()
                }
                // The box: a signed read of the owner's own turn is the turn as minted; else hidden.
                val body = answer(if (signed) minted("owner_signed") else minted("hidden"))
                return MockResponse.Builder().code(200).body(body).build()
            }
        }
        server.start()
        val k = OriginSigner.encodeKey(ByteArray(32) { (it * 7 + 3).toByte() })
        val origin = server.url("").toString()
        WellnessChatSigning.installForTest {
            WellnessSigningInterceptor.Facts(WellnessBindingRecord(op, "en_0f1c0f1c", 1, k, boxOrigin = origin),
                WellnessSyncState(state = WellnessRules.EnrollmentState.ACTIVE.wire), origin)
        }
    }

    @After fun tearDown() {
        WellnessChatSigning.installForTest(null)
        Dispatchers.resetMain()
        server.close()
    }

    private fun vm(screen: String) = TimelineViewModel(Application()).apply {
        screenOperator = { screen }
        initialize(server.url("").toString().trimEnd('/'))
    }

    private fun awaitPeek(vm: TimelineViewModel, snapId: String): SnapshotPeek {
        val deadline = System.currentTimeMillis() + 10_000
        while (System.currentTimeMillis() < deadline) {
            vm.peek.value?.takeIf { it.snapId == snapId && !it.loading }?.let { return it }
            Thread.sleep(10)
        }
        fail("the peek never loaded; requests=${requests.map { it.target }}")
        throw AssertionError()
    }

    @Test
    fun `the owner's reloaded health turn comes back masked with its gauge, and Show numbers is offered`() {
        val vm = vm(op)
        vm.navigateToSnapshot("SNAP-20261030-001")
        val peek = awaitPeek(vm, "SNAP-20261030-001")
        val text = peek.content!!
        assertNotNull(requests.single().headers[OriginSigner.HEADER])
        assertEquals("/fossil/snapshot/SNAP-20261030-001", requests.single().target)
        assertTrue(WellnessChatRules.isMasked(text))
        assertTrue(WellnessChatRules.offersShowNumbers(text, emptyList(), vm.selectedSnapshot.value!!.operator, op))
        assertEquals(listOf(minted("ref")), WellnessChatRules.showNumbersRefs(text))
    }

    @Test
    fun `a refused proof is asked once more unsigned, so the Timeline still shows the turn`() {
        refuseProof = 1
        val vm = vm(op)
        vm.navigateToSnapshot("SNAP-20261030-001")
        val peek = awaitPeek(vm, "SNAP-20261030-001")
        assertNull(peek.error)
        assertEquals(minted("hidden"), peek.content)
        assertEquals(2, requests.size)
        assertNotNull(requests[0].headers[OriginSigner.HEADER])
        assertNull(requests[1].headers[OriginSigner.HEADER])
        assertFalse(WellnessChatRules.offersShowNumbers(peek.content, emptyList(), op, op))
    }

    @Test
    fun `another operator on screen reads the turn unsigned, as before Health`() {
        val vm = vm("someone-else")
        vm.navigateToSnapshot("SNAP-20261030-001")
        val peek = awaitPeek(vm, "SNAP-20261030-001")
        assertNull(requests.single().headers[OriginSigner.HEADER])
        assertEquals(minted("hidden"), peek.content)
        assertFalse(WellnessChatRules.offersShowNumbers(peek.content, emptyList(), op, op))
    }

    @Test
    fun `the detail view's full-content fetch is the same signed read`() {
        val vm = vm(op)
        vm.selectSnapshot(com.aiblackbox.portal.data.model.SnapshotResult(snapId = "SNAP-20261030-001", operator = op,
            timestamp = "", snippet = "", type = ""))
        val deadline = System.currentTimeMillis() + 10_000
        while (vm.fullContent.value.isNullOrEmpty() && System.currentTimeMillis() < deadline) Thread.sleep(10)
        assertEquals(minted("owner_signed"), vm.fullContent.value)
        assertNotNull(requests.single().headers[OriginSigner.HEADER])
    }
}
