package com.aiblackbox.portal.ui.cli_agent

import org.junit.Assert.assertEquals
import org.junit.Test

class PreviewTailTest {

    @Test
    fun short_text_is_returned_unchanged() {
        assertEquals("hello world", previewTail("hello world", max = 160))
    }

    @Test
    fun text_at_exactly_max_is_unchanged() {
        val text = "x".repeat(50)
        assertEquals(text, previewTail(text, max = 50))
    }

    @Test
    fun long_text_keeps_the_tail_with_a_leading_ellipsis() {
        // 21-char tail we want to keep visible
        val tail = "the most recent words"
        val text = "a".repeat(100) + tail
        assertEquals("…$tail", previewTail(text, max = tail.length))
    }
}
