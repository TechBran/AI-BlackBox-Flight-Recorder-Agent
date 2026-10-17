package com.aiblackbox.portal.data.voice

import com.aiblackbox.portal.data.api.WsMessage
import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotSame
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.TimeUnit

/**
 * The dictation mic's teardown: WHO releases the `AudioRecord`, and WHEN.
 *
 * WHAT WAS BROKEN. Four places released the recorder from a thread that was not reading it: stop()
 * on the caller's thread (the CLI mic tap, on main), shutdown() via a re-press, the connection loop
 * when a socket leg ended, and the capture loop's own finally — which released the SHARED field, so
 * an old leg's loop could free a NEWER leg's recorder under that leg's read. Any one of them landing
 * while read() is in flight is the native abort in `AudioRecord::releaseBuffer`
 * (feedback_android_audiorecord_race). The fix is the voice screen's rule: everything else only
 * SIGNALS; each leg's loop releases its own recorder in its own finally, on the thread that reads it.
 *
 * [FakeAudioRecord] parks every read() until the test feeds it, so "a read is in flight" is a state
 * the test holds open on purpose rather than a timing accident. [FakeWebSocketClient] stands in for
 * each socket leg.
 */
class SttStreamClientRaceTest {

    private val sockets = CopyOnWriteArrayList<FakeWebSocketClient>()
    private val records = CopyOnWriteArrayList<FakeAudioRecord>()

    private val stt = SttStreamClient(
        OkHttpClient(),
        "ws://box.test",
        unprocessedSupported = false,
        wsFactory = { FakeWebSocketClient().also { sockets += it } },
        recordFactory = { _, sampleRate, _ -> FakeAudioRecord(sampleRate).also { records += it } },
    )

    private val caller: Thread = Thread.currentThread()

    @After
    fun tearDown() {
        // Free anything a failing run left parked, so it cannot hold an IO thread into the next test.
        stt.stop()
        sockets.forEach { it.incoming.trySend(WsMessage.Text(STT_DONE)) }
        records.forEach { rec -> repeat(3) { rec.feed(0) } }
    }

    /** The [n]th item of a list another thread appends to. */
    private fun <T> awaitNth(list: List<T>, n: Int, what: String): T {
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(FakeAudioRecord.WAIT_SECONDS)
        while (list.size <= n) {
            assertTrue("timed out waiting for $what", System.nanoTime() < deadline)
            Thread.sleep(5)
        }
        return list[n]
    }

    /** Open socket leg [leg]; return the recorder its capture loop is now parked in a read() on. */
    private fun connect(leg: Int, record: Int): FakeAudioRecord {
        awaitNth(sockets, leg, "socket leg $leg").incoming.trySend(WsMessage.Connected)
        val rec = awaitNth(records, record, "recorder $record")
        assertTrue("recorder $record's capture loop never reached read()", rec.awaitRead())
        return rec
    }

    private fun assertReleasedOnceByItsOwnLoop(name: String, rec: FakeAudioRecord) {
        assertEquals("$name released exactly once", 1, rec.releaseThreads.size)
        val releaser = rec.releaseThreads.single()
        assertNotSame("$name released on the caller's thread", caller, releaser)
        assertTrue("$name released on a thread that never read it", releaser in rec.readThreads)
        assertFalse("$name released while a read() was in flight", rec.releasedDuringRead)
    }

    @Test
    fun `stop only signals - the capture loop releases the recorder once, on its own reading thread`() {
        stt.start()
        val rec = connect(leg = 0, record = 0)

        // A read is in flight right now. On a device, releasing under it is a native abort.
        stt.stop()

        assertTrue(
            "stop() released the AudioRecord on the caller's thread while read() was in flight",
            rec.releaseThreads.isEmpty(),
        )
        assertEquals("stop() must not touch the AudioRecord at all — it only signals", 0, rec.stopCount.get())
        assertFalse("stop() must report the session as over at once", stt.isStreaming.value)

        rec.feed(1) // the in-flight read returns — AFTER the stop
        assertTrue("the capture loop never released its recorder", rec.awaitRelease())
        sockets[0].incoming.trySend(WsMessage.Text(STT_DONE))
        Thread.sleep(SETTLE_MS) // room for a second, wrong release (teardown, connection loop) to show up

        assertReleasedOnceByItsOwnLoop("the recorder", rec)
        assertEquals("stopped exactly once, by the loop", 1, rec.stopCount.get())
    }

    @Test
    fun `a dropped socket leg does not release the recorder under the capture loop's read`() {
        stt.start()
        val first = connect(leg = 0, record = 0)

        // Transport drop mid-dictation: the connection loop ends this leg and reconnects.
        sockets[0].incoming.close()
        awaitNth(sockets, 1, "the reconnect leg") // the connection loop is past its end-of-leg teardown

        assertTrue(
            "the connection loop released the AudioRecord while read() was in flight",
            first.releaseThreads.isEmpty(),
        )
        first.feed(1)
        assertTrue("the dropped leg's capture loop never released its recorder", first.awaitRelease())

        // The session itself survives the drop: the next leg opens its OWN recorder.
        val second = connect(leg = 1, record = 1)
        assertTrue("the new leg's recorder was released with the old one", second.releaseThreads.isEmpty())

        stt.stop()
        second.feed(1)
        assertTrue(second.awaitRelease())
        sockets[1].incoming.trySend(WsMessage.Text(STT_DONE))
        Thread.sleep(SETTLE_MS)

        assertReleasedOnceByItsOwnLoop("the dropped leg's recorder", first)
        assertReleasedOnceByItsOwnLoop("the resumed leg's recorder", second)
    }

    @Test
    fun `a quick stop then start never lets the old session's loop read or release the new session's recorder`() {
        stt.start()
        val first = connect(leg = 0, record = 0)

        stt.stop()
        stt.start() // re-press while the old read is still in flight: start() shuts the old session down
        assertTrue(
            "stop()/shutdown() released the old AudioRecord on the caller's thread while read() was in flight",
            first.releaseThreads.isEmpty(),
        )
        assertTrue("start() did not report the new session as streaming", stt.isStreaming.value)

        val second = connect(leg = 1, record = 1)
        assertNotEquals("the new session must open its own recorder", first, second)

        // The old session's in-flight read returns only now, with the new session already reading.
        first.feed(1)
        assertTrue("the old session's loop never released its recorder", first.awaitRelease())
        Thread.sleep(SETTLE_MS)

        assertTrue(
            "the old session's loop released the NEW session's recorder (the shared field)",
            second.releaseThreads.isEmpty(),
        )
        assertEquals("only the new session's own loop may read its recorder", 1, second.readThreads.size)
        assertEquals(1, second.maxConcurrentReads)

        second.feed(1)
        assertTrue("the new session's loop died with the old one", second.awaitRead())

        stt.stop()
        assertTrue("stop() released the AudioRecord on the caller's thread", second.releaseThreads.isEmpty())
        second.feed(1)
        assertTrue(second.awaitRelease())
        sockets[1].incoming.trySend(WsMessage.Text(STT_DONE))
        Thread.sleep(SETTLE_MS)

        assertReleasedOnceByItsOwnLoop("the old session's recorder", first)
        assertReleasedOnceByItsOwnLoop("the new session's recorder", second)
    }

    private companion object {
        const val SETTLE_MS = 100L
        const val STT_DONE = """{"type":"stt_done"}"""
    }
}
