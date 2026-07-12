package com.aiblackbox.portal.data.repository

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * parseXaiVoicesResponse — the GET /xai/voices contract the xAI Voice Lab zone
 * consumes: {configured, voices:[{voice_id|id, name}]}. Tolerant of both id key
 * namings (probe P6.20) and sparse rows.
 */
class XaiVoiceParsingTest {

    @Test
    fun `parses configured with voices`() {
        val raw = """{"configured": true, "voices": [
            {"voice_id": "cv-1", "name": "Narrator"},
            {"id": "cv-2", "name": "Alt"}
        ]}"""
        val res = parseXaiVoicesResponse(raw)
        assertTrue(res.configured)
        assertEquals(2, res.voices.size)
        assertEquals(XaiVoice("cv-1", "Narrator"), res.voices[0])
        assertEquals(XaiVoice("cv-2", "Alt"), res.voices[1])
    }

    @Test
    fun `unconfigured yields empty`() {
        val res = parseXaiVoicesResponse("""{"configured": false, "voices": []}""")
        assertFalse(res.configured)
        assertTrue(res.voices.isEmpty())
    }

    @Test
    fun `row without any id is skipped and missing name falls back to id`() {
        val raw = """{"configured": true, "voices": [
            {"name": "no-id-row"},
            {"voice_id": "cv-3"}
        ]}"""
        val res = parseXaiVoicesResponse(raw)
        assertEquals(1, res.voices.size)
        assertEquals(XaiVoice("cv-3", "cv-3"), res.voices[0])
    }

    @Test
    fun `garbage-free on unknown extra keys`() {
        val raw = """{"configured": true, "extra": 1, "voices": [
            {"voice_id": "cv-4", "name": "V", "created_at": "2026-07-11"}
        ]}"""
        assertEquals(1, parseXaiVoicesResponse(raw).voices.size)
    }
}
