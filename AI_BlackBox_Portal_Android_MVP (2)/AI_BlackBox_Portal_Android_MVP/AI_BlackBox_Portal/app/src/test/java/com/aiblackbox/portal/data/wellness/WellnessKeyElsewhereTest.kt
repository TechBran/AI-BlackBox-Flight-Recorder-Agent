package com.aiblackbox.portal.data.wellness

import com.aiblackbox.portal.data.wellness.WellnessRules.EnrollmentState
import com.aiblackbox.portal.data.wellness.WellnessRules.NarrowingOutcome
import com.aiblackbox.portal.data.wellness.WellnessRules.StatusChip
import kotlinx.coroutines.runBlocking
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import java.io.File

/**
 * A phone whose key was made with one box address while the app now uses another (contract
 * (f)): re-pointed at another box, or the same box reached under a new name. The key signs
 * nothing for the paired box, and that is VISIBLE and recoverable: the chip needs attention, a
 * card names both addresses and offers setting Health up with this box or forgetting it here,
 * a narrowing says no box was told (never "Couldn't reach your box"), and the collector, the
 * capability report and the history import all stand still. Real binding, real IngestClient,
 * a stand-in for the paired box that must receive nothing signed.
 */
class WellnessKeyElsewhereTest {

    private val op = WellnessFixtures.OPERATOR
    private val boxA = "http://100.64.0.10:9091"
    private val boxB = MockWebServer()
    private val files = WellnessFiles(WellnessFixtures.tempDir("elsewhere"))
    private val binding = WellnessBinding(files)
    private val k = OriginSigner.encodeKey(ByteArray(32) { (it * 5 + 2).toByte() })

    @Before fun setUp() = boxB.start()

    @After fun tearDown() = boxB.close()

    private val paired get() = boxB.url("").toString().removeSuffix("/")

    /** Health set up with [origin]: the bind is pinned to it. */
    private fun bindWith(origin: String, enr: String = "en_0a0b0c0d", epoch: Int = 1) {
        val intent = binding.beginIntent(op, origin)
        assertTrue(binding.acceptBind(WellnessBindRequest(op, enr, epoch, intent.nonce, k)) is BindDecision.Accepted)
    }

    /** The app's client for the box it is paired with now, its key use pinned as WellnessGraph wires it. */
    private fun clientForPaired() = IngestClient(paired, nowSec = { 1_793_000_000L },
        keyPinned = { url -> binding.mayUseKeyFor(url, paired) })

    @Test
    fun `an app re-pointed at another box shows why, and tells that box nothing`() = runBlocking {
        bindWith(boxA)
        val client = clientForPaired()
        val signer = binding.signer()!!
        val disconnect = client.action(IngestClient.DISCONNECT, """{"operator":"$op","keep_history":true}""", signer)
        val erase = client.action(IngestClient.ERASE, """{"operator":"$op"}""", signer)
        val pause = client.action(IngestClient.PAUSE, """{"operator":"$op"}""", signer)
        val status = client.status(op, signer)
        assertEquals("nothing reached the paired box", 0, boxB.requestCount)
        for (r in listOf(disconnect, erase, pause, status)) {
            assertEquals(-1, r.status)
            assertEquals(WellnessChatRules.KEY_OTHER_BOX, r.error)
            assertEquals(NarrowingOutcome.KEY_ELSEWHERE, WellnessRules.narrowingOutcome(r.status, r.error))
            assertEquals("never 'Couldn't reach your box'", WellnessRules.KEY_ELSEWHERE_SHORT,
                WellnessRules.unsentMessage(r.reached, r.error, "The box didn't accept that."))
        }

        // The screen: both addresses named, needs attention (never Active), whatever the phone last saw.
        val elsewhere = WellnessRules.keyElsewhereOf(binding.current()!!.boxOrigin, paired)
        assertEquals(WellnessRules.KeyElsewhere(boxA, paired), elsewhere)
        val words = WellnessRules.keyElsewhereMessage(elsewhere!!)
        assertTrue(boxA in words && paired in words)
        assertEquals(StatusChip.NEEDS_ATTENTION,
            WellnessRules.statusChip(EnrollmentState.ACTIVE, true, emptyList(), keyElsewhere = true))
        assertEquals(StatusChip.NEEDS_ATTENTION,
            WellnessRules.statusChip(EnrollmentState.ACTIVE, true, emptyList(), held = true, keyElsewhere = true))

        // Disconnect, keep history: forgotten here. A delete keeps the key (only it can delete
        // that store) and says nothing was deleted. Pause stays a pause on this phone.
        assertEquals(WellnessRules.ElsewhereNarrowing(true, WellnessRules.FORGOT_HERE_MESSAGE),
            WellnessRules.elsewhereNarrowing(WellnessRules.HOLD_DISCONNECT, keepHistory = true))
        assertEquals(WellnessRules.ElsewhereNarrowing(false, WellnessRules.NOT_DELETED_ELSEWHERE_MESSAGE),
            WellnessRules.elsewhereNarrowing(WellnessRules.HOLD_DISCONNECT, keepHistory = false))
        assertEquals(WellnessRules.ElsewhereNarrowing(false, WellnessRules.NOT_DELETED_ELSEWHERE_MESSAGE),
            WellnessRules.elsewhereNarrowing(WellnessRules.HOLD_ERASE, keepHistory = null))
        assertEquals(WellnessRules.ElsewhereNarrowing(false, WellnessRules.PAUSED_ELSEWHERE_MESSAGE),
            WellnessRules.elsewhereNarrowing(WellnessRules.HOLD_PAUSE, keepHistory = null))

        // Nothing is collected or reported for a box that cannot receive it.
        val st = WellnessSyncState(state = EnrollmentState.ACTIVE.wire)
        assertFalse(WellnessRules.mayReportCapabilities(st, keyElsewhere = true))
        assertFalse(WellnessRules.shouldStartBackfill(false, false, false, EnrollmentState.ACTIVE, keyElsewhere = true))
        val gate = WellnessRules.maySync(WellnessRules.SyncInputs(bound = true, state = EnrollmentState.ACTIVE,
            sdk = HcSdkStatus.AVAILABLE, grantedReadTypes = 3, inForeground = true, backgroundGranted = true,
            stopped = false, outboxFull = false, backoffUntilMs = 0L, nowMs = 1L, keyElsewhere = true))
        assertEquals(WellnessRules.SyncDecision.Skip(WellnessRules.KEY_ELSEWHERE), gate)
        assertFalse(WellnessRules.mayDrainWhenSkipped(WellnessRules.KEY_ELSEWHERE))
        assertTrue("the binding stays until the person chooses", binding.current() != null)
    }

    @Test
    fun `the same box under a new address is elsewhere too, but a cosmetic difference is not`() {
        // A tailnet IP and the box's https name are two origins: the phone cannot know they are one box.
        assertNotNull(WellnessRules.keyElsewhereOf(boxA, "https://blackbox.example.ts.net"))
        assertNotNull(WellnessRules.keyElsewhereOf("https://blackbox.example.ts.net", boxA))
        assertNotNull(WellnessRules.keyElsewhereOf(boxA, "https://100.64.0.10:9091"))
        assertNotNull(WellnessRules.keyElsewhereOf(boxA, "http://100.64.0.10:9092"))
        // the same origin written differently is the same box
        assertNull(WellnessRules.keyElsewhereOf(boxA, "$boxA/"))
        assertNull(WellnessRules.keyElsewhereOf(boxA, "$boxA/ui/"))
        assertNull(WellnessRules.keyElsewhereOf("https://blackbox.example.ts.net:443", "https://blackbox.example.ts.net"))
        // an older binding adopts the paired box (never elsewhere); an unpaired app is another trouble
        assertNull(WellnessRules.keyElsewhereOf(null, boxA))
        assertNull(WellnessRules.keyElsewhereOf("  ", boxA))
        assertNull(WellnessRules.keyElsewhereOf(boxA, null))
        assertFalse(WellnessRules.keyElsewhere(boxA, boxA))
        assertTrue(WellnessRules.keyElsewhere(boxA, "https://blackbox.example.ts.net"))
    }

    @Test
    fun `setting Health up with this box pins the new key to it, and forgetting it here leaves nothing`() = runBlocking {
        bindWith(boxA)
        assertNotNull(WellnessRules.keyElsewhereOf(binding.current()!!.boxOrigin, paired))
        // "Set up Health with this box": the Reconnect setup's enroll call goes to the paired box.
        bindWith(paired, enr = "en_1a2b3c4d", epoch = 2)
        assertEquals(WellnessChatRules.originOf(paired), binding.current()!!.boxOrigin)
        assertNull(WellnessRules.keyElsewhereOf(binding.current()!!.boxOrigin, paired))
        boxB.enqueue(MockResponse.Builder().code(200).body("""{"state":"active"}""").build())
        val r = clientForPaired().status(op, binding.signer()!!)
        assertEquals(200, r.status)
        assertNotNull("the new key signs for this box", boxB.takeRequest().headers[OriginSigner.HEADER])

        // "Forget Health on this phone": the key, settings and outbox go; no box is asked.
        val before = boxB.requestCount
        binding.clearAll()
        assertNull(binding.current())
        assertNull(WellnessRules.keyElsewhereOf(binding.current()?.boxOrigin, paired))
        assertEquals(StatusChip.NOT_SET_UP, WellnessRules.statusChip(EnrollmentState.ACTIVE, false, emptyList()))
        assertEquals(before, boxB.requestCount)
    }

    // ── the wiring ─────────────────────────────────────────────────────────────────────────

    private fun source(rel: String): String {
        var dir: File? = File(".").absoluteFile
        repeat(8) {
            val d = dir ?: return@repeat
            for (candidate in listOf(File(d, rel), File(d, "app/$rel"))) if (candidate.isFile) return candidate.readText()
            dir = d.parentFile
        }
        fail("could not locate $rel from ${File(".").absolutePath}")
        error("unreachable")
    }

    private val pkg = "src/main/java/com/aiblackbox/portal/data/wellness"

    @Test
    fun `the capability report, the import and the collector ask the same rules before they read`() {
        val graph = source("$pkg/WellnessGraph.kt")
        val push = graph.substringAfter("suspend fun pushCapabilities(").substringBefore("\n    }\n")
        val gate = push.indexOf("WellnessRules.mayReportCapabilities(st, keyElsewhere(app))")
        assertTrue("pushCapabilities asks mayReportCapabilities", gate > 0)
        assertTrue("before Health Connect is asked anything", gate < push.indexOf("grantedPermissions()"))
        assertTrue("before the probe reads", gate < push.indexOf("probe(app).run()"))
        assertTrue("the engine knows the paired box", "pairedOrigin = { boxOrigin(app) }" in graph)

        val workers = source("$pkg/WellnessWorkers.kt")
        val wanted = workers.substringAfter("fun backfillWanted(").substringBefore(")\n")
        assertTrue("the import waits for the policy", "policyHold = WellnessRules.policyHold(st)" in workers)
        assertTrue(wanted, "keyElsewhere: Boolean" in wanted)
        assertEquals("every caller passes the key's box", 2, Regex("backfillWanted\\(.*WellnessGraph\\.keyElsewhere\\(app\\)\\)")
            .findAll(workers).count())
        val enrolled = workers.substringAfter("fun onEnrolled(").substringBefore("\n    }\n")
        assertTrue("setup under a newer box policy does not start the import", "if (!WellnessRules.policyHold(st)) startBackfill" in enrolled)
    }

    @Test
    fun `the Health screen shows the key's box and its narrowings never say the box was unreachable`() {
        val vm = source("src/main/java/com/aiblackbox/portal/ui/wellness/WellnessViewModel.kt")
        assertTrue("the screen learns it", "WellnessRules.keyElsewhereOf(record?.boxOrigin, WellnessGraph.boxOrigin(app))" in vm)
        assertTrue("the chip hears it", "keyElsewhere = elsewhere != null" in vm)
        assertTrue("no policy card for another box's text", "elsewhere == null && WellnessRules.policyHold(st)" in vm)
        val narrow = vm.substringAfter("private fun narrow(").substringBefore("\n    }\n")
        assertTrue("the outcome reads the error", "WellnessRules.narrowingOutcome(reply.status, reply.error)" in narrow)
        assertTrue("and acts on it", "WellnessRules.elsewhereNarrowing(hold, keepHistory)" in narrow)
        assertTrue("setup agrees to the text it showed", "WellnessRules.enrolledUnderPolicy(it, outcome.policyVersion)" in vm)
        val screen = source("src/main/java/com/aiblackbox/portal/ui/wellness/WellnessScreen.kt")
        assertTrue("the card is on the screen", "state.keyElsewhere?.let { KeyElsewhereCard(it, viewModel) }" in screen)
        val card = screen.substringAfter("private fun KeyElsewhereCard(").substringBefore("\n}\n")
        assertTrue("set up with this box", "viewModel.startSetup()" in card)
        assertTrue("or forget it here", "viewModel.forgetHere()" in card)
    }
}
