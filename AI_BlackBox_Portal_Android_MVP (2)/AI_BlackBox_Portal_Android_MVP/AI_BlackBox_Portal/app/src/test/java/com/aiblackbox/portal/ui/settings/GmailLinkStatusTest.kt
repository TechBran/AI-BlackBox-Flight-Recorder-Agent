package com.aiblackbox.portal.ui.settings

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Audit W15: GET /gmail/status/{operator} reports a revoked/expired Google grant
 * as connected:false + state "needs_reauth" + reauth_url. Settings must turn that
 * into a Reconnect action (it used to read "Connected" for a dead token), while a
 * healthy token keeps the existing connected flow and an older backend without
 * `state` still parses from the boolean.
 */
class GmailLinkStatusTest {

    private val origin = "https://box.example.ts.net"

    @Test fun `a dead grant parses as needs reauth with the server reauth path`() {
        val s = GmailLinkStatus.parse(
            """{"connected":false,"email":"","operator":"Robin","state":"needs_reauth",
               "needs_reauth":true,"error":"invalid_grant","error_message":"...",
               "reauth_url":"/auth/gmail/authorize?operator=Robin"}"""
        )
        assertEquals(GmailLinkStatus.State.NEEDS_REAUTH, s.state)
        assertFalse(s.isConnected)
        assertEquals("invalid_grant", s.error)
        assertEquals("$origin/auth/gmail/authorize?operator=Robin", s.authorizeUrl(origin, "Robin"))
    }

    @Test fun `a healthy token keeps the connected flow`() {
        val s = GmailLinkStatus.parse(
            """{"connected":true,"email":"robin@example.com","operator":"Robin","state":"connected",
               "needs_reauth":false,"error":null,"error_message":null,"reauth_url":null}"""
        )
        assertEquals(GmailLinkStatus.State.CONNECTED, s.state)
        assertTrue(s.isConnected)
        assertEquals("robin@example.com", s.email)
        assertNull(s.error)
        assertNull(s.reauthPath)
    }

    @Test fun `an unverifiable token still counts as connected`() {
        val s = GmailLinkStatus.parse("""{"connected":true,"email":"","state":"unverified","error":"probe_failed"}""")
        assertEquals(GmailLinkStatus.State.UNVERIFIED, s.state)
        assertTrue(s.isConnected)
    }

    @Test fun `no token is not connected`() {
        val s = GmailLinkStatus.parse("""{"connected":false,"email":"","state":"not_connected"}""")
        assertEquals(GmailLinkStatus.State.NOT_CONNECTED, s.state)
        assertFalse(s.isConnected)
    }

    @Test fun `an older backend without state falls back to the boolean`() {
        assertEquals(
            GmailLinkStatus.State.CONNECTED,
            GmailLinkStatus.parse("""{"connected":true,"email":"a@example.com","operator":"Robin"}""").state,
        )
        assertEquals(
            GmailLinkStatus.State.NOT_CONNECTED,
            GmailLinkStatus.parse("""{"connected":false,"email":"","operator":"Robin"}""").state,
        )
    }

    @Test fun `a foreign reauth url is never followed`() {
        val s = GmailLinkStatus.parse(
            """{"connected":false,"state":"needs_reauth","reauth_url":"https://evil.example/phish"}"""
        )
        assertEquals("$origin/auth/gmail/authorize?operator=Robin", s.authorizeUrl(origin, "Robin"))
    }

    @Test fun `the built authorize url encodes the operator and tolerates a trailing slash`() {
        assertEquals(
            "$origin/auth/gmail/authorize?operator=Flight+Recorder",
            GmailLinkStatus.authorizeUrl("$origin/", "Flight Recorder"),
        )
    }
}
