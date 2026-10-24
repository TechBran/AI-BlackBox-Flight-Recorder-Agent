package com.aiblackbox.portal.ui.timeline

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

/**
 * A07-components-rest-01: the snapshot peek sheet showed snapshot A's body under
 * snapshot B's id while B loaded, and kept it for good when B's fetch failed. The body
 * is now tagged with the id it was fetched for, and [peekViewFor] only ever renders a
 * body whose tag matches the id on the sheet's title.
 */
class SnapshotPeekTest {

    @Test fun `a body fetched for another snapshot is never shown`() {
        val peek = SnapshotPeek(snapId = "SNAP-A", content = "body of A", loading = false)
        assertEquals(PeekView.Loading, peekViewFor(peek, currentSnapId = "SNAP-B"))
    }

    @Test fun `a failed fetch shows an error, not the previous body`() {
        val peek = SnapshotPeek(snapId = "SNAP-B", content = null, loading = false, error = "Could not load SNAP-B")
        assertEquals(PeekView.Error("Could not load SNAP-B"), peekViewFor(peek, "SNAP-B"))
    }

    @Test fun `a loading snapshot shows the spinner`() {
        assertEquals(PeekView.Loading, peekViewFor(SnapshotPeek(snapId = "SNAP-B"), "SNAP-B"))
        assertEquals(PeekView.Loading, peekViewFor(null, "SNAP-B"))
    }

    @Test fun `the matching body is shown`() {
        val peek = SnapshotPeek(snapId = "SNAP-B", content = "body of B", loading = false)
        assertEquals(PeekView.Body("body of B"), peekViewFor(peek, "SNAP-B"))
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

    @Test fun `navigating cancels the stale fetch and the sheet renders through peekViewFor`() {
        val vm = source("src/main/java/com/aiblackbox/portal/ui/timeline/TimelineViewModel.kt")
        val nav = vm.substring(vm.indexOf("fun navigateToSnapshot("))
        assertTrue("a newer peek cancels the older fetch", "peekJob?.cancel()" in nav.substring(0, nav.indexOf("launch")))
        val sheet = source("src/main/java/com/aiblackbox/portal/ui/components/SnapshotPeekSheet.kt")
        assertTrue("the sheet renders through the tagged peek", "peekViewFor(" in sheet)
    }
}
