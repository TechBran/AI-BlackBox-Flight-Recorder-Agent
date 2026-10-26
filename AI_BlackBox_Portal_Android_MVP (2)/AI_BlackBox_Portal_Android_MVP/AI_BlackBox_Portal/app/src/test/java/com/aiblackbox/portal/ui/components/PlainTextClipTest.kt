package com.aiblackbox.portal.ui.components

import android.content.ClipDescription
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

/**
 * The copy buttons moved from the deprecated LocalClipboardManager.setText(AnnotatedString)
 * to LocalClipboard.setClipEntry(plainTextClipEntry(text)). The pasted payload must not
 * change: one plain-text item, the exact characters, the label Compose always used.
 *
 * ClipData is framework code, which the plain-JVM harness stubs to null
 * (unitTests.returnDefaultValues), so this runs on Robolectric's real android.jar.
 */
@RunWith(RobolectricTestRunner::class)
class PlainTextClipTest {

    @Test
    fun `the clip carries exactly the copied text as one plain-text item`() {
        // Markdown, emoji with a ZWJ sequence, CRLF and trailing spaces: nothing is
        // normalised, styled or trimmed on the way to the clipboard.
        val text = "## Reply\r\n```kotlin\nval x = 1\n```\n👩‍💻 done  "

        val clip = plainTextClipEntry(text).clipData

        assertEquals(1, clip.itemCount)
        assertEquals(text, clip.getItemAt(0).text.toString())
        // The literal AndroidClipboardManager.setText used, not the constant: pinned on purpose.
        assertEquals("plain text", clip.description.label.toString())
        assertTrue(clip.description.hasMimeType(ClipDescription.MIMETYPE_TEXT_PLAIN))
    }

    @Test
    fun `an empty reply copies as an empty plain-text item`() {
        val clip = plainTextClipEntry("").clipData

        assertEquals(1, clip.itemCount)
        assertEquals("", clip.getItemAt(0).text.toString())
    }
}
