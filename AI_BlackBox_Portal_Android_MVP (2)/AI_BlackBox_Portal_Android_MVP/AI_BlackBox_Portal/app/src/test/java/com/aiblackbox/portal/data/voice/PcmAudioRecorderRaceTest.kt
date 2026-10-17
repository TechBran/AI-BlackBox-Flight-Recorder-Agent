package com.aiblackbox.portal.data.voice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.util.concurrent.CopyOnWriteArrayList

/**
 * The composer recorder's teardown: WHO releases the `AudioRecord`, and WHEN.
 *
 * WHAT WAS BROKEN. stop() released the recorder on the caller's thread (the composer tap, on main)
 * while the IO capture loop was blocked inside read() — the cross-thread read()/release() race that
 * aborts natively in `AudioRecord::releaseBuffer` (feedback_android_audiorecord_race). The fix is
 * the voice screen's rule: stop() only SIGNALS; the loop releases its own recorder in its own
 * finally, on the thread that reads it.
 *
 * WHY THE SECOND TEST. A stop() that only signals is not enough on its own. If the loop keeps
 * testing a SHARED flag and writing through the writer FIELD, a quick stop() + start() flips the
 * flag back on and swaps the writer before the old loop's in-flight read returns — so the old take
 * keeps reading and appends its audio into the NEW take's file. Each take therefore owns its
 * recorder, its writer and its stop signal.
 *
 * [FakeAudioRecord] parks every read() until the test feeds it, so "a read is in flight" is a state
 * the test holds open on purpose rather than a timing accident.
 */
class PcmAudioRecorderRaceTest {

    @get:Rule
    val temp = TemporaryFolder()

    private val records = CopyOnWriteArrayList<FakeAudioRecord>()

    private fun recorder() = PcmAudioRecorder(
        temp.root,
        unprocessedSupported = false,
        recordFactory = { _, sampleRate, _ -> FakeAudioRecord(sampleRate).also { records += it } },
    )

    /** One read's worth of bytes — what the capture loop asks every read() for. */
    private val chunkBytes =
        MicBuffer.framesForMs(PcmAudioRecorder.READ_MS, PcmAudioRecorder.SAMPLE_RATE) * MicBuffer.BYTES_PER_SAMPLE

    private fun payload(file: File): ByteArray =
        file.readBytes().copyOfRange(WavStreamWriter.HEADER_BYTES, file.length().toInt())

    @Test
    fun `stop only signals - the capture loop releases the recorder once, on its own reading thread`() {
        val rec = recorder()
        assertTrue(rec.start())
        val take = records.single()
        assertTrue("the capture loop never reached read()", take.awaitRead())
        take.feed(0x11)
        assertTrue("the capture loop never came back for a second read()", take.awaitRead())
        val caller = Thread.currentThread()

        // A read is in flight right now. On a device, releasing under it is a native abort.
        val file = rec.stop()

        assertNotNull(file)
        assertTrue(
            "stop() released the AudioRecord on the caller's thread while read() was in flight",
            take.releaseThreads.isEmpty(),
        )
        assertEquals("stop() must not touch the AudioRecord at all — it only signals", 0, take.stopCount.get())
        assertFalse("stop() must report the take as over at once", rec.isRecording)

        take.feed(0x22) // the in-flight read returns — AFTER the take was stopped
        assertTrue("the capture loop never released its recorder", take.awaitRelease())
        Thread.sleep(SETTLE_MS) // room for a second, wrong release to show up

        assertEquals("released exactly once", 1, take.releaseThreads.size)
        val releaser = take.releaseThreads.single()
        assertNotEquals("released on the caller's thread", caller, releaser)
        assertTrue("released on a thread that never read it", releaser in take.readThreads)
        assertFalse("released while a read() was in flight", take.releasedDuringRead)
        assertEquals("stopped exactly once, by the loop", 1, take.stopCount.get())

        // The file stop() returned is finished: the read that landed after it is not in it.
        val bytes = payload(file!!)
        assertEquals(chunkBytes, bytes.size)
        assertTrue(bytes.all { it == 0x11.toByte() })
    }

    @Test
    fun `a quick stop then start never writes the old take's in-flight audio into the new take`() {
        val rec = recorder()
        assertTrue(rec.start())
        val first = records[0]
        assertTrue(first.awaitRead())
        first.feed(0x11)
        assertTrue(first.awaitRead()) // take 1's chunk is written; its next read is in flight

        val firstFile = rec.stop()!!
        // Take files are named by the millisecond they start in; step past it so the two takes
        // cannot collide on one name. The race window is held open by the parked read, not by time.
        Thread.sleep(2)
        assertTrue(rec.start())
        val second = records[1]
        assertTrue("the new take's loop never reached read()", second.awaitRead())

        // Take 1's in-flight read returns only now, with take 2 already recording.
        first.feed(0x11)
        assertTrue("the old take never released its recorder", first.awaitRelease())

        second.feed(0x22)
        assertTrue("the new take's loop died with the old one", second.awaitRead())
        val secondFile = rec.stop()!!
        second.feed(0x22)
        assertTrue("the new take never released its recorder", second.awaitRelease())
        Thread.sleep(SETTLE_MS)

        assertNotEquals(firstFile, secondFile)
        val firstBytes = payload(firstFile)
        val secondBytes = payload(secondFile)
        assertEquals("take 1 holds its one chunk", chunkBytes, firstBytes.size)
        assertTrue(firstBytes.all { it == 0x11.toByte() })
        assertEquals("take 2 holds exactly its own one chunk", chunkBytes, secondBytes.size)
        assertTrue("take 1's audio leaked into take 2", secondBytes.all { it == 0x22.toByte() })

        val caller = Thread.currentThread()
        for ((name, take) in listOf("take 1" to first, "take 2" to second)) {
            assertEquals("$name released exactly once", 1, take.releaseThreads.size)
            assertNotEquals("$name released on the caller's thread", caller, take.releaseThreads.single())
            assertFalse("$name released while a read() was in flight", take.releasedDuringRead)
        }
        assertEquals("only take 2's own loop may ever read take 2's recorder", 1, second.readThreads.size)
        assertEquals(1, second.maxConcurrentReads)
    }

    private companion object {
        const val SETTLE_MS = 100L
    }
}
