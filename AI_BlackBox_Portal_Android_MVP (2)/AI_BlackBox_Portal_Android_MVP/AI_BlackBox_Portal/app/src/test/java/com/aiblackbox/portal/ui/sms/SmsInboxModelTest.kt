package com.aiblackbox.portal.ui.sms

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.fail
import org.junit.Test
import java.io.IOException
import java.time.ZoneId
import java.time.ZoneOffset
import java.time.ZonedDateTime
import java.util.Locale

/**
 * The SMS inbox's pure rules (audit rank 54):
 *  - A14-ui-misc-10: the thread list is keyed by phone, and the box could list one phone
 *    twice (two rows sharing its newest timestamp), which crashes a LazyColumn on the
 *    duplicate key. Threads are deduped by the box's thread key.
 *  - A14-ui-misc-04: the Send button cleared the draft before the request finished, so a
 *    failed SMS was silently lost. A draft is cleared only after its send succeeded.
 *  - A14-ui-misc-11: bubble times showed "22+00" (the tail of an ISO string) and thread
 *    times raw UTC. Both are local wall-clock times now.
 */
class SmsInboxModelTest {

    // ── thread key ───────────────────────────────────────────────────────────

    @Test fun `the thread key is the box's rule, the last 10 digits`() {
        // Orchestrator/sms/message_store.py _normalize_phone
        assertEquals("5550100123", SmsPhone.threadKey("+1 (555) 010-0123"))
        assertEquals("5550100123", SmsPhone.threadKey("+15550100123"))
        assertEquals("5550100123", SmsPhone.threadKey("5550100123"))
        assertEquals("22000", SmsPhone.threadKey("22000"))
        assertEquals("", SmsPhone.threadKey(""))
    }

    // ── thread list ──────────────────────────────────────────────────────────

    @Test fun `a phone the box lists twice is shown once, newest row first`() {
        val raw = """{"threads":[
            {"phone_number":"5550100123","contact_name":"Pat","last_message":"the reply","last_timestamp":"2026-10-01T12:00:00+00:00","unread_count":1,"direction":"outbound","line_number":""},
            {"phone_number":"5550100123","contact_name":"Pat","last_message":"part of a burst","last_timestamp":"2026-10-01T12:00:00+00:00","unread_count":1,"direction":"inbound","line_number":""},
            {"phone_number":"+15550100123","last_message":"same thread, other spelling"},
            {"phone_number":"5550100222","last_message":"other","unread_count":"x"}
        ]}"""
        val threads = parseSmsThreads(raw)
        assertEquals(listOf("5550100123", "5550100222"), threads.map { it.phoneNumber })
        assertEquals("the reply", threads[0].lastMessage)
        assertEquals("", threads[1].contactName)
        assertEquals(0, threads[1].unreadCount)
        assertEquals(threads.size, threads.map { SmsPhone.threadKey(it.phoneNumber) }.toSet().size)
    }

    @Test fun `an unreadable thread list is empty, not a crash`() {
        assertEquals(emptyList<SmsThread>(), parseSmsThreads("{}"))
        assertEquals(emptyList<SmsThread>(), parseSmsThreads("""{"threads":[1, "x", null]}"""))
    }

    // ── drafts ───────────────────────────────────────────────────────────────

    @Test fun `a failed send keeps the draft to retry`() = runBlocking {
        val drafts = SmsDrafts()
        drafts.set("5550100123", "on my way \\ see you")
        assertThrows(IOException::class.java) {
            runBlocking { drafts.send("5550100123") { throw IOException("Not connected to AMI") } }
        }
        assertEquals("on my way \\ see you", drafts.of("5550100123"))
    }

    @Test fun `a successful send clears the draft`() = runBlocking {
        val drafts = SmsDrafts()
        drafts.set("5550100123", "  hello\nthere  ")
        var sent: String? = null
        val result = drafts.send("5550100123") { sent = it }
        assertEquals("hello\nthere", sent)
        assertEquals("hello\nthere", result)
        assertEquals("", drafts.of("5550100123"))
    }

    @Test fun `text typed while the send was in flight is kept`() = runBlocking {
        val drafts = SmsDrafts()
        drafts.set("5550100123", "first")
        drafts.send("5550100123") { drafts.set("5550100123", "first, and more") }
        assertEquals("first, and more", drafts.of("5550100123"))
    }

    @Test fun `a blank draft sends nothing`() = runBlocking {
        val drafts = SmsDrafts()
        drafts.set("5550100123", "   ")
        assertNull(drafts.send("5550100123") { fail("nothing to send") })
    }

    @Test fun `drafts belong to their thread, however the number is written`() {
        val drafts = SmsDrafts()
        drafts.set("+1 (555) 010-0123", "for Pat")
        drafts.set("5550100222", "for Lee")
        assertEquals("for Pat", drafts.of("5550100123"))
        assertEquals("for Lee", drafts.of("+15550100222"))
        drafts.clear()
        assertEquals("", drafts.of("5550100123"))
    }

    // ── times ────────────────────────────────────────────────────────────────

    private val newYork: ZoneId = ZoneId.of("America/New_York")
    private val us = Locale.US

    /** Newer CLDR data puts a narrow no-break space before AM/PM. */
    private fun plain(s: String) = s.replace(' ', ' ').replace(' ', ' ')

    @Test fun `a bubble shows the local wall-clock time, not the tail of the ISO string`() {
        assertEquals("10:03 AM", plain(SmsTime.clock("2026-10-08T14:03:22+00:00", newYork, us)))
        assertEquals("10:03 AM", plain(SmsTime.clock("2026-10-08T14:03:22.123456+00:00", newYork, us)))
        assertEquals("2:03 PM", plain(SmsTime.clock("2026-10-08T14:03:22+00:00", ZoneOffset.UTC, us)))
        assertEquals("14:03", SmsTime.clock("2026-10-08T14:03:22+00:00", ZoneOffset.UTC, Locale.GERMANY))
        assertEquals("", SmsTime.clock("", newYork, us))
        assertEquals("", SmsTime.clock("not a time", newYork, us))
    }

    @Test fun `a thread row shows a local time for today and a date before that`() {
        val now = ZonedDateTime.of(2026, 10, 8, 18, 0, 0, 0, newYork)
        assertEquals("10:03 AM", plain(SmsTime.threadStamp("2026-10-08T14:03:22+00:00", newYork, now, us)))
        assertEquals("Oct 7, 10:03 AM", plain(SmsTime.threadStamp("2026-10-07T14:03:22+00:00", newYork, now, us)))
        assertEquals("", SmsTime.threadStamp("garbage", newYork, now, us))
    }
}
