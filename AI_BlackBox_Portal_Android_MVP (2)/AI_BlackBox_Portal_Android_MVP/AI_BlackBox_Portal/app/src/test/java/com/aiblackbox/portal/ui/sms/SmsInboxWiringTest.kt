package com.aiblackbox.portal.ui.sms

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

/**
 * WIRING guard for the SMS screen and its view model, which cannot be stood up on the
 * plain JVM (a Composable, an AndroidViewModel). The rules themselves are tested where
 * they live (SmsInboxModelTest, SmsInboxApiTest); this pins that the screen uses them
 * (audit rank 54):
 *  - A14-ui-misc-04: one SnackbarHost above both views (a conversation had none, so
 *    "Failed to send" never showed while it was open), one-shot messages instead of a
 *    nullable StateFlow (an identical second message never re-emitted), and the draft
 *    cleared only by a successful send;
 *  - A14-ui-misc-10: the thread list is deduped and keyed by the box's thread key;
 *  - the box now serves a thread's newest page, whose size stays the same as messages
 *    arrive, so the conversation follows its newest message id, not its size, and
 *    decides from the list the reader last saw (SmsFollow), not from scroll events: its
 *    own animateScrollToItem counts as a scroll in progress, so a flag set from them
 *    stopped the following after the first automatic scroll;
 *  - A14-ui-misc-11: bubble and thread times go through SmsTime.
 */
class SmsInboxWiringTest {

    private fun source(rel: String): String {
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            for (candidate in listOf(File(dir, rel), File(dir, "app/$rel"))) {
                if (candidate.isFile) return candidate.readText()
            }
            dir = dir.parentFile
        }
        fail("could not locate $rel from ${File(".").absolutePath}")
        return ""
    }

    private val root = "src/main/java/com/aiblackbox/portal/ui/sms"
    private val screen by lazy { source("$root/SmsInboxScreen.kt") }
    private val viewModel by lazy { source("$root/SmsViewModel.kt") }

    /** From `fun <name>(` to the next top-level or member function. */
    private fun functionBody(src: String, name: String): String {
        val start = Regex("fun $name\\(").find(src)?.range?.first ?: -1
        assertTrue("fun $name( not found", start >= 0)
        val next = Regex("\n(?:    )?(?:@Composable\n)?(?:(?:private|internal|override|suspend|public) )*fun ")
            .find(src, start + 1)?.range?.first ?: src.length
        return src.substring(start, next)
    }

    @Test fun `one snackbar host serves the thread list and the conversation`() {
        assertEquals("exactly one SnackbarHost", 1, Regex("""\bSnackbarHost\(""").findAll(screen).count())
        val top = functionBody(screen, "SmsInboxScreen")
        assertTrue("the host is composed by SmsInboxScreen itself", "SnackbarHost(" in top)
        assertFalse("the list view no longer owns a host", "snackbarHostState" in functionBody(screen, "SmsThreadListView"))
    }

    @Test fun `messages are one-shot events, collected once`() {
        assertTrue("a Channel carries them", Regex("""Channel<String>\(""").containsMatchIn(viewModel))
        assertFalse("not the nullable StateFlow it was", Regex("""\b_actionMessage\b""").containsMatchIn(viewModel))
        assertTrue(functionBody(screen, "SmsInboxScreen").contains("actionMessages.collect"))
    }

    @Test fun `the draft lives in the view model and only a successful send clears it`() {
        val conv = functionBody(screen, "SmsConversationView")
        assertFalse("the screen never clears the draft itself", Regex("""inputText\s*=\s*""""").containsMatchIn(conv))
        assertFalse("no draft held only by the composable", "var inputText" in conv)
        val send = functionBody(viewModel, "sendSms")
        assertTrue(send, "drafts.send(" in send)
    }

    @Test fun `threads are deduped and keyed by the thread key`() {
        assertTrue(functionBody(viewModel, "loadThreads").contains("parseSmsThreads("))
        assertTrue(Regex("""items\(threads, key = \{ SmsPhone\.threadKey\(it\.phoneNumber\) \}\)""").containsMatchIn(screen))
    }

    @Test fun `the conversation follows its newest message, not its size`() {
        val conv = functionBody(screen, "SmsConversationView")
        assertFalse("LaunchedEffect(messages.size)" in conv)
        assertTrue(conv, Regex("""LaunchedEffect\(phone, newestId\)""").containsMatchIn(conv))
    }

    @Test fun `following is decided from the list the reader saw, not from scroll events`() {
        val conv = functionBody(screen, "SmsConversationView")
        assertFalse("no scroll-in-progress flag", "isScrollInProgress" in conv)
        assertFalse("snapshotFlow" in conv)
        assertTrue(conv, "SmsFollow.shouldScroll(" in conv)
        assertTrue(conv, "listState.layoutInfo" in conv)
        assertTrue(conv, ".visibleItemsInfo" in conv)
        assertTrue(conv, ".viewportEndOffset" in conv)
    }

    @Test fun `times are shown in local wall-clock time`() {
        assertFalse("takeLast(8)" in screen)
        assertTrue("SmsTime.clock(" in functionBody(screen, "SmsBubble"))
        assertTrue("SmsTime.threadStamp(" in functionBody(screen, "SmsThreadCard"))
    }
}
