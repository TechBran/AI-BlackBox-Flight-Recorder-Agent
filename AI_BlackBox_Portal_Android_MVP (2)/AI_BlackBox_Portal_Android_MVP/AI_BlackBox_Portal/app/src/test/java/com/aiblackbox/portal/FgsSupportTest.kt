package com.aiblackbox.portal

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

/**
 * Audit rank 60 (A01-activities-root-02, -06, -09): every foreground service
 *  - starts foreground through [FgsSupport.startForegroundSafely] (a platform refusal on
 *    a background START_STICKY restart is caught instead of crashing the process),
 *  - implements onTimeout (Android 15 stops a dataSync service at its 6-hour limit and
 *    kills the app if it does not stop itself), and
 *  - posts under an id from [NotificationIds], so no two notifications overwrite or
 *    remove each other (the model download and the terminal service both used 9094; a
 *    download id could land on the keep-alive service's 9999).
 */
class FgsSupportTest {

    @Test fun `every fixed notification id is distinct`() {
        val ids = NotificationIds.FIXED
        assertEquals("duplicate notification ids: $ids", ids.size, ids.toSet().size)
    }

    @Test fun `download ids never land on a fixed id`() {
        val fixed = NotificationIds.FIXED.toSet()
        for (now in listOf(0L, 999L, 1_000L, 9_999L, 123_456_789L, Long.MAX_VALUE)) {
            val id = NotificationIds.downloadId(now)
            assertFalse("download id $id collides", id in fixed)
            assertTrue(id in NotificationIds.DOWNLOAD_BASE until NotificationIds.DOWNLOAD_BASE + NotificationIds.DOWNLOAD_SPAN)
        }
    }

    @Test fun `one-off alert ids stay clear of the fixed ids and the download band`() {
        val fixed = NotificationIds.FIXED.toSet()
        for (now in listOf(0L, 9_999L, 20_500L, 1_760_000_000_000L, Long.MAX_VALUE)) {
            val id = NotificationIds.generalId(now)
            assertFalse(id in fixed)
            assertFalse(id in NotificationIds.DOWNLOAD_BASE until NotificationIds.DOWNLOAD_BASE + NotificationIds.DOWNLOAD_SPAN)
            assertTrue("ids are positive", id > 0)
        }
    }

    // ── wiring ───────────────────────────────────────────────────────────────

    private fun source(rel: String): String {
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            for (candidate in listOf(File(dir, rel), File(dir, "app/$rel"))) {
                if (candidate.isFile) return candidate.readText().replace("\r\n", "\n")
            }
            dir = dir.parentFile
        }
        fail("could not locate $rel")
        return ""
    }

    private val services = listOf(
        "KeepAliveService", "BackgroundTaskService", "ModelDownloadService",
        "LocalModelService", "TerminalForegroundService", "NotificationListenerFgs",
    )

    @Test fun `every foreground service starts safely, handles its timeout and uses a registered id`() {
        for (name in services) {
            val src = source("src/main/java/com/aiblackbox/portal/$name.kt")
            assertTrue("$name starts foreground through FgsSupport", "FgsSupport.startForegroundSafely(" in src)
            assertFalse("$name has no unguarded ServiceCompat.startForeground", "ServiceCompat.startForeground(" in src)
            assertFalse("$name has no bare startForeground(", Regex("(?<![.\\w])startForeground\\(").containsMatchIn(src))
            assertTrue("$name stops on its time limit", "override fun onTimeout(startId: Int, fgsType: Int)" in src)
            assertTrue("$name posts under a registered id", "NotificationIds." in src)
        }
    }

    // ── the download's time limit (review follow-up) ─────────────────────────

    @Test fun `a download stopped at the time limit says so, not that it was cancelled`() {
        // onTimeout cancels the download; its catch turns the CancellationException into a
        // failure whose message is the generic "...was cancelled", published LAST.
        val cancelled = Result.failure<Any>(kotlinx.coroutines.CancellationException("StandaloneCoroutine was cancelled"))
        val state = ModelDownloadService.terminalDownloadState("gemma", cancelled, timedOut = true)
        assertEquals(com.aiblackbox.portal.data.local.DownloadProgressBus.Status.FAILED, state.status)
        assertEquals(ModelDownloadService.TEXT_TIMED_OUT, state.error)
    }

    @Test fun `other download outcomes are published as before`() {
        val ok = ModelDownloadService.terminalDownloadState("gemma", Result.success(Unit), timedOut = true)
        assertEquals("a download that finished keeps its result", com.aiblackbox.portal.data.local.DownloadProgressBus.Status.SUCCESS, ok.status)
        val failed = ModelDownloadService.terminalDownloadState("gemma", Result.failure<Any>(java.io.IOException("disk full")), timedOut = false)
        assertEquals("disk full", failed.error)
    }

    @Test fun `the download publishes its terminal state through the time-limit aware helper`() {
        val src = source("src/main/java/com/aiblackbox/portal/ModelDownloadService.kt")
        assertTrue("DownloadProgressBus.update(terminalDownloadState(bundle.slug, result, timedOut))" in src)
        val timeout = src.substring(src.indexOf("override fun onTimeout("))
        assertTrue("the flag is set before the cancel", timeout.indexOf("timedOut = true") in 0 until timeout.indexOf("scope.cancel()"))
    }
}
