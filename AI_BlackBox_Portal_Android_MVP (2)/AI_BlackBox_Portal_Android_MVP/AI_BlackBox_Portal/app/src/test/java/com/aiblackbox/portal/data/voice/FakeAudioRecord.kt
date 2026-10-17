package com.aiblackbox.portal.data.voice

import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.CountDownLatch
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.Semaphore
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/**
 * A JVM stand-in for `android.media.AudioRecord`, built to make the read()/release() race visible.
 *
 * THE RACE. A capture loop blocks inside `read()` for up to one buffer period; a `release()` from
 * any OTHER thread during that window frees the buffer the read is still filling, and the audio HAL
 * aborts natively in `AudioRecord::releaseBuffer` (feedback_android_audiorecord_race). On a device
 * that is a SIGABRT, not an exception — which is why nothing caught it.
 *
 * HOW THIS SHOWS IT. [read] PARKS until the test calls [feed], so the test controls exactly when a
 * read is in flight. [release] records the calling thread and whether a read was parked at that
 * moment ([releasedDuringRead]) — the condition that is a native crash on a real device. [read] also
 * records every thread that reads, and the peak number of concurrent reads, so a straggler loop
 * reading a NEWER take's recorder shows up as a second reader.
 *
 * Constructible on the JVM because the unit-test android.jar returns defaults instead of throwing
 * (`unitTests.returnDefaultValues`); every method the capture paths call is overridden here.
 */
class FakeAudioRecord(private val rate: Int) : AudioRecord(
    MediaRecorder.AudioSource.MIC,
    rate,
    AudioFormat.CHANNEL_IN_MONO,
    AudioFormat.ENCODING_PCM_16BIT,
    4096,
) {
    /** One entry per read the test lets complete: the byte every sample of that read is filled with. */
    private val chunks = LinkedBlockingQueue<Int>()
    private val inRead = AtomicInteger(0)

    /** Released once per read() that has entered and is about to park. */
    val readEntered = Semaphore(0)
    val readThreads: MutableSet<Thread> = ConcurrentHashMap.newKeySet()
    @Volatile var maxConcurrentReads = 0
        private set

    val stopCount = AtomicInteger(0)
    val releaseThreads: MutableList<Thread> = CopyOnWriteArrayList()
    /** Counted down on the first release() — what a test waits on for the loop's own teardown. */
    val released = CountDownLatch(1)
    @Volatile var releasedDuringRead = false
        private set

    /** Let exactly one parked (or future) read() complete, every sample set to [fill]. */
    fun feed(fill: Byte) {
        chunks.put(fill.toInt())
    }

    /** Wait for a read() to be in flight — the window the race lives in. */
    fun awaitRead(): Boolean = readEntered.tryAcquire(WAIT_SECONDS, TimeUnit.SECONDS)

    fun awaitRelease(): Boolean = released.await(WAIT_SECONDS, TimeUnit.SECONDS)

    override fun getState(): Int = STATE_INITIALIZED

    override fun getSampleRate(): Int = rate

    override fun getRecordingState(): Int = RECORDSTATE_RECORDING

    override fun startRecording() {}

    override fun stop() {
        stopCount.incrementAndGet()
    }

    override fun release() {
        if (inRead.get() > 0) releasedDuringRead = true
        releaseThreads += Thread.currentThread()
        released.countDown()
        // A real release under a parked read aborts the process; here the read just ends, so a
        // test that exposes the race fails on its assertions instead of hanging.
        chunks.put(POISON)
    }

    override fun read(audioData: ByteArray, offsetInBytes: Int, sizeInBytes: Int): Int {
        val fill = park() ?: return ERROR_DEAD_OBJECT
        java.util.Arrays.fill(audioData, offsetInBytes, offsetInBytes + sizeInBytes, fill.toByte())
        return sizeInBytes
    }

    override fun read(audioData: ShortArray, offsetInShorts: Int, sizeInShorts: Int): Int {
        val fill = park() ?: return ERROR_DEAD_OBJECT
        java.util.Arrays.fill(audioData, offsetInShorts, offsetInShorts + sizeInShorts, fill.toShort())
        return sizeInShorts
    }

    /** The fill byte for this read, or null when the recorder was released under it. */
    private fun park(): Int? {
        val now = inRead.incrementAndGet()
        if (now > maxConcurrentReads) maxConcurrentReads = now
        readThreads += Thread.currentThread()
        try {
            readEntered.release()
            // Bounded, so a broken run fails its assertions instead of parking the suite forever.
            val fill = chunks.poll(WAIT_SECONDS, TimeUnit.SECONDS)
            return if (fill == null || fill == POISON) null else fill
        } finally {
            inRead.decrementAndGet()
        }
    }

    companion object {
        const val WAIT_SECONDS = 5L
        private const val POISON = Int.MIN_VALUE
    }
}
