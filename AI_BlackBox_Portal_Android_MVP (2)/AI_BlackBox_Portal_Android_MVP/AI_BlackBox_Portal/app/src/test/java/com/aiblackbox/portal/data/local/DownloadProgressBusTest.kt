package com.aiblackbox.portal.data.local

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Before
import org.junit.Test

/**
 * Unit tests for [DownloadProgressBus] — the pure-Kotlin process bus the
 * foreground download Service publishes to and the ViewModel observes.
 */
class DownloadProgressBusTest {

    @Before
    fun reset() {
        // Isolate from any state a prior test (or the object's process lifetime) left.
        DownloadProgressBus.clearAll()
    }

    @Test
    fun `update then observe latest by slug`() {
        DownloadProgressBus.update(
            DownloadProgressBus.State("gemma-4-e4b", 0.5f, DownloadProgressBus.Status.RUNNING),
        )
        val s = DownloadProgressBus.flow.value["gemma-4-e4b"]!!
        assertEquals(0.5f, s.fraction, 0.0001f)
        assertEquals(DownloadProgressBus.Status.RUNNING, s.status)
    }

    @Test
    fun `clear removes a slug`() {
        DownloadProgressBus.update(
            DownloadProgressBus.State("x", 1f, DownloadProgressBus.Status.SUCCESS),
        )
        DownloadProgressBus.clear("x")
        assertNull(DownloadProgressBus.flow.value["x"])
    }
}
