package com.aiblackbox.portal.data.voice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class TranscriptMergeTest {
    @Test fun `merge interleaves by timestamp`() {
        val server = listOf(
            TranscriptEntry("user", "hi", timestamp = 100),
            TranscriptEntry("assistant", "hello", timestamp = 300),
        )
        val local = listOf(TranscriptEntry("tool_call", "🔧 search_snapshots", timestamp = 200))
        assertEquals(listOf("hi", "🔧 search_snapshots", "hello"),
            mergeTranscript(server, local).map { it.text })
    }

    @Test fun `chip roles are everything except user and assistant`() {
        assertTrue(isChipRole("tool_call"))
        assertTrue(isChipRole("image_task"))
        assertFalse(isChipRole("user"))
        assertFalse(isChipRole("assistant"))
    }

    @Test fun `tool chip labels`() {
        assertEquals("🔧 search_snapshots", toolChipText("tool_call", "search_snapshots", ""))
        assertEquals("✔ search_snapshots — 3 hits", toolChipText("tool_result", "search_snapshots", "3 hits"))
        assertTrue(toolChipText("image_task", "", "sunset over water").contains("image"))
        assertTrue(toolChipText("music_task", "", "").contains("music"))
    }
}
