package com.aiblackbox.portal.ui.webview

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

/**
 * Audit rank 62 (A14-ui-misc-01, A13-ui-ops-11): the app's WebViews had no
 * WebChromeClient (confirm() returned false, alert() vanished, <input type=file> never
 * opened a picker, so the onboarding wizard could not remove an operator or upload a
 * credential file) and no onRenderProcessGone (a renderer crash killed the whole app).
 * One helper, used by all three WebViews.
 *
 * The WebView callbacks need a device; this pins the exactly-once delivery rule they
 * rest on (a file chooser or JS dialog that never answers wedges the page) and the
 * wiring of every WebView.
 */
class BbxWebViewTest {

    @Test fun `a chooser or dialog result is delivered exactly once`() {
        val got = mutableListOf<String?>()
        val once = OnceValueCallback<String> { got += it }
        assertTrue(once.complete("picked"))
        assertFalse("a second answer is dropped", once.complete(null))
        assertEquals(listOf<String?>("picked"), got)
    }

    @Test fun `a cancelled or failed chooser still answers, with null`() {
        val got = mutableListOf<String?>()
        val once = OnceValueCallback<String> { got += it }
        assertTrue(once.complete(null))
        assertEquals(listOf<String?>(null), got)
    }

    private fun source(rel: String): String {
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            for (candidate in listOf(File(dir, rel), File(dir, "app/$rel"))) {
                if (candidate.isFile) return candidate.readText()
            }
            dir = dir.parentFile
        }
        fail("could not locate $rel")
        return ""
    }

    private val root = "src/main/java/com/aiblackbox/portal"

    @Test fun `all three WebViews get the chrome client and the renderer-crash guard`() {
        for (rel in listOf("ui/webview/WizardWebViewScreen.kt", "ui/cu/CuLiveViewScreen.kt", "PortalActivity.kt")) {
            val src = source("$root/$rel")
            assertTrue("$rel has the shared chrome client", "BbxWebChromeClient(" in src)
            assertTrue("$rel survives a renderer crash", "BbxWebViewClient(" in src)
            assertFalse("$rel keeps no bare WebViewClient()", "= WebViewClient()" in src)
        }
    }

    @Test fun `the wizard can pick files and both compose WebViews remount after a renderer crash`() {
        val wizard = source("$root/ui/webview/WizardWebViewScreen.kt")
        assertTrue("the wizard's file inputs open a picker", "rememberWebFileChooser()" in wizard)
        for (rel in listOf("ui/webview/WizardWebViewScreen.kt", "ui/cu/CuLiveViewScreen.kt")) {
            assertTrue("$rel remounts a fresh WebView", "key(rendererGeneration)" in source("$root/$rel"))
        }
    }

    // ── review follow-up: a renderer that dies on every load is not reloaded forever ──

    @Test fun `a crash or two is replaced at once, a crash loop waits for a tap`() {
        var now = 0L
        val budget = RendererCrashBudget(maxAutoReplacements = 2, windowMs = 60_000L, now = { now })
        assertTrue(budget.recordCrash())
        now += 1_000
        assertTrue(budget.recordCrash())
        now += 1_000
        assertFalse("the third crash in a minute stops the reload loop", budget.recordCrash())
        assertFalse("and so does every one after it", budget.recordCrash())

        budget.reset() // the user tapped Reload
        assertTrue(budget.recordCrash())
    }

    @Test fun `crashes far apart are each replaced`() {
        var now = 0L
        val budget = RendererCrashBudget(maxAutoReplacements = 2, windowMs = 60_000L, now = { now })
        repeat(10) {
            assertTrue("crash $it", budget.recordCrash())
            now += 61_000
        }
    }

    @Test fun `every WebView host stops replacing a renderer that keeps crashing`() {
        for (rel in listOf("ui/webview/WizardWebViewScreen.kt", "ui/cu/CuLiveViewScreen.kt")) {
            val src = source("$root/$rel")
            assertTrue("$rel counts crashes", "if (crashBudget.recordCrash()) rendererGeneration++ else rendererCrashed = true" in src)
            assertTrue("$rel shows a Reload state", "RendererCrashedPanel(" in src)
        }
        val portal = source("$root/PortalActivity.kt")
        val gone = portal.substring(portal.indexOf("private fun onPortalRendererGone()"))
        assertTrue("PortalActivity recreates only within the budget", "if (portalRendererCrashes.recordCrash())" in gone.substring(0, 300))
        assertTrue("PortalActivity's client goes through it", "BbxWebViewClient(onRendererGone = { onPortalRendererGone() })" in portal)
    }
}
