package com.aiblackbox.portal

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * A01-activities-root-01 / X04-security-clients-infra-05 — the legacy WebView
 * ([PortalActivity]) keeps navigation, its JS bridges (mic, downloads, overlay,
 * file picker) and the mic permission grant on the PAIRED box origin only.
 * Before, any link in a chat reply navigated the WebView to a third-party page
 * that then owned every bridge, and the mic grant matched any https origin or
 * anything containing ".ts.net" (foo.ts.net.evil.example matched).
 *
 * The comparison is the web origin: scheme + host + effective port.
 */
class PortalWebGuardTest {

    // What PortalActivity loads for a tailnet pairing (normalizeOrigin output).
    private val tailnet = PortalWebGuard.originOf("https://box.tail0000.ts.net/ui/")
    // What it loads for a LAN pairing.
    private val lan = PortalWebGuard.originOf("http://192.168.1.50:9091/ui/")

    @Test fun `origin is scheme, host and effective port`() {
        assertEquals("https://box.tail0000.ts.net", tailnet)
        assertEquals("http://192.168.1.50:9091", lan)
        assertEquals("https://box.tail0000.ts.net", PortalWebGuard.originOf("https://box.tail0000.ts.net:443/ui/"))
        assertEquals("http://localhost:9091", PortalWebGuard.originOf("http://localhost:9091"))
        assertEquals("http://[::1]:9091", PortalWebGuard.originOf("http://[::1]:9091/ui/"))
    }

    @Test fun `the paired origin matches whatever the path, query or fragment`() {
        assertTrue(PortalWebGuard.isPairedOrigin("https://box.tail0000.ts.net/ui/", tailnet))
        assertTrue(PortalWebGuard.isPairedOrigin("https://box.tail0000.ts.net/cu/view/abc?x=1#top", tailnet))
        assertTrue(PortalWebGuard.isPairedOrigin("http://192.168.1.50:9091/app-proxy/8070/", lan))
    }

    @Test fun `a different port is a different origin`() {
        // A user App served directly on its own port is foreign code: no bridges.
        assertFalse(PortalWebGuard.isPairedOrigin("http://192.168.1.50:8070/", lan))
        assertFalse(PortalWebGuard.isPairedOrigin("http://192.168.1.50/", lan))
        assertFalse(PortalWebGuard.isPairedOrigin("https://box.tail0000.ts.net:8443/ui/", tailnet))
    }

    @Test fun `http and https are different origins`() {
        assertFalse(PortalWebGuard.isPairedOrigin("http://box.tail0000.ts.net/ui/", tailnet))
        assertFalse(PortalWebGuard.isPairedOrigin("https://192.168.1.50:9091/ui/", lan))
    }

    @Test fun `userinfo never changes the host`() {
        // The paired name in the userinfo slot: the host is the attacker's.
        assertFalse(PortalWebGuard.isPairedOrigin("https://box.tail0000.ts.net@evil.example/ui/", tailnet))
        assertFalse(PortalWebGuard.isPairedOrigin("https://box.tail0000.ts.net:443@evil.example/", tailnet))
        // A backslash ends the authority (as in the browser): the host is evil.example.
        assertFalse(PortalWebGuard.isPairedOrigin("https://evil.example\\@box.tail0000.ts.net/", tailnet))
        // Credentials in front of the paired host still load the paired box.
        assertTrue(PortalWebGuard.isPairedOrigin("https://someone@box.tail0000.ts.net/ui/", tailnet))
    }

    @Test fun `look-alike hosts do not match`() {
        assertFalse(PortalWebGuard.isPairedOrigin("https://box.tail0000.ts.net.evil.example/ui/", tailnet))
        assertFalse(PortalWebGuard.isPairedOrigin("https://evilbox.tail0000.ts.net/ui/", tailnet))
        assertFalse(PortalWebGuard.isPairedOrigin("https://x.box.tail0000.ts.net/ui/", tailnet))
        assertFalse(PortalWebGuard.isPairedOrigin("https://other.tail0000.ts.net/ui/", tailnet))
    }

    @Test fun `case does not matter`() {
        assertTrue(PortalWebGuard.isPairedOrigin("HTTPS://BOX.Tail0000.TS.NET/ui/", tailnet))
        assertEquals(tailnet, PortalWebGuard.originOf("https://BOX.TAIL0000.TS.NET/ui/"))
    }

    @Test fun `a trailing dot is the same DNS name`() {
        assertTrue(PortalWebGuard.isPairedOrigin("https://box.tail0000.ts.net./ui/", tailnet))
        assertEquals(tailnet, PortalWebGuard.originOf("https://box.tail0000.ts.net./ui/"))
    }

    @Test fun `a blob URL has the origin of the page that made it`() {
        assertTrue(PortalWebGuard.isPairedOrigin("blob:https://box.tail0000.ts.net/0f2c-uuid", tailnet))
        assertFalse(PortalWebGuard.isPairedOrigin("blob:https://evil.example/0f2c-uuid", tailnet))
    }

    @Test fun `non-web schemes and junk have no origin and never match`() {
        for (url in listOf(
            null, "", "   ", "about:blank", "javascript:alert(1)", "data:text/html,<b>x</b>",
            "file:///sdcard/Download/x.html", "content://com.aiblackbox.portal.fileprovider/x",
            "intent://scan/#Intent;scheme=zxing;end", "mailto:someone@example.com", "tel:5550100",
            "https://", "not a url",
        )) {
            assertNull(url, PortalWebGuard.originOf(url))
            assertFalse(url, PortalWebGuard.isPairedOrigin(url, tailnet))
        }
    }

    @Test fun `with no paired origin nothing matches`() {
        assertFalse(PortalWebGuard.isPairedOrigin("https://box.tail0000.ts.net/ui/", null))
        assertFalse(PortalWebGuard.isPairedOrigin(null, null))
    }

    @Test fun `download names cannot climb out of Downloads`() {
        assertEquals("evil.sh", PortalWebGuard.safeDownloadName("../../evil.sh"))
        assertEquals("b.txt", PortalWebGuard.safeDownloadName("a\\b.txt"))
        assertEquals("report.pdf", PortalWebGuard.safeDownloadName("report.pdf"))
        assertEquals("download", PortalWebGuard.safeDownloadName(""))
        assertEquals("download", PortalWebGuard.safeDownloadName(".."))
        assertEquals("download", PortalWebGuard.safeDownloadName("dir/"))
    }
}
