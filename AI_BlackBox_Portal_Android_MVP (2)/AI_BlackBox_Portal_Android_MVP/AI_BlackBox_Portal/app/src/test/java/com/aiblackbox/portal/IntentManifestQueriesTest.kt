package com.aiblackbox.portal

import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Guards the Android 11+ PACKAGE-VISIBILITY posture for the comprehensive intent
 * layer (decision 9 / M1.5). The frontier model must be able to RESOLVE and LAUNCH
 * arbitrary installed apps + deep links via the intent path (and the gesture layer's
 * `open_app`, which uses `getLaunchIntentForPackage` — gated by this visibility).
 *
 * We deliberately chose a BROAD-BUT-HONEST `<queries>` (LAUNCHER + BROWSABLE
 * http/https + the concrete schemes/actions the actuator fires) over the
 * Play-flagged sensitive `QUERY_ALL_PACKAGES` permission. This test reads the real
 * `AndroidManifest.xml` off disk and pins both halves of that choice so a future
 * edit can't silently drop the queries or reach for `QUERY_ALL_PACKAGES`.
 *
 * Pure (no Android framework) — reads the manifest as text on the host JVM.
 * (Named with the `Intent` prefix so it runs under the M1.5 `*Intent*` test filter.)
 */
class IntentManifestQueriesTest {

    /** Locate + read `AndroidManifest.xml` regardless of the unit-test working dir. */
    private fun manifestText(): String {
        val direct = listOf(
            File("src/main/AndroidManifest.xml"),
            File("app/src/main/AndroidManifest.xml"),
        )
        direct.firstOrNull { it.exists() }?.let { return it.readText() }
        // Walk up from the working dir looking for the module manifest.
        var dir: File? = File("").absoluteFile
        repeat(8) {
            val d = dir ?: return@repeat
            for (rel in listOf("src/main/AndroidManifest.xml", "app/src/main/AndroidManifest.xml")) {
                val f = File(d, rel)
                if (f.exists()) return f.readText()
            }
            dir = d.parentFile
        }
        throw AssertionError("AndroidManifest.xml not found from ${File("").absolutePath}")
    }

    @Test
    fun `queries block declares broad launcher and browsable visibility plus core deep-link schemes`() {
        val m = manifestText()
        assertTrue("a <queries> block must be present", m.contains("<queries>"))
        // BROAD: resolve/launch ANY installed launchable app.
        assertTrue("MAIN action present", m.contains("android.intent.action.MAIN"))
        assertTrue("LAUNCHER category present", m.contains("android.intent.category.LAUNCHER"))
        // BROAD: ANY http/https deep-link handler visible.
        assertTrue("BROWSABLE category present", m.contains("android.intent.category.BROWSABLE"))
        // Core deep-link schemes fired by the intent actuator.
        for (scheme in listOf("tel", "geo", "smsto", "mailto", "google.navigation", "https", "http")) {
            assertTrue("scheme '$scheme' declared in <queries>", m.contains("android:scheme=\"$scheme\""))
        }
    }

    @Test
    fun `queries block covers the new decision-9 intent actions`() {
        val m = manifestText()
        for (action in listOf(
            "android.media.action.VIDEO_CAPTURE",
            "android.media.action.MEDIA_PLAY_FROM_SEARCH",
            "android.intent.action.SHOW_ALARMS",
            "android.intent.action.OPEN_DOCUMENT",
            "android.intent.action.CREATE_DOCUMENT",
            "android.intent.action.GET_CONTENT",
            "android.intent.action.PICK",
        )) {
            assertTrue("action '$action' declared in <queries>", m.contains(action))
        }
    }

    @Test
    fun `manifest prefers broad queries over the sensitive QUERY_ALL_PACKAGES permission`() {
        // Check the actual permission FQN in a declaration — NOT the bare token, which
        // legitimately appears in the <queries> explanatory comment.
        assertTrue(
            "android.permission.QUERY_ALL_PACKAGES must NOT be declared — decision 9 prefers a broad-but-honest <queries>",
            !manifestText().contains("android.permission.QUERY_ALL_PACKAGES"),
        )
    }
}
