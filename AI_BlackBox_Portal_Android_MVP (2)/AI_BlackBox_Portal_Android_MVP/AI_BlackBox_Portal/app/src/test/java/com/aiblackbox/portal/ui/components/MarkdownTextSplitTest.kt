package com.aiblackbox.portal.ui.components

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Tests for [splitContent]'s unfenced-JSON extraction (Phase A1 of the
 * reply-parsing-and-rendering-hardening plan). A large/multi-line JSON blob
 * embedded in prose must be lifted out into a CodeBlock(language="json") so it
 * is not fed to the markdown parser (which mangles JSON-significant chars).
 * Detection is conservative: parse-gated, multi-line OR >= 80 chars only.
 */
class MarkdownTextSplitTest {

    private fun codeBlocks(content: String) =
        splitContent(content).filterIsInstance<ContentSegment.CodeBlock>()

    private fun markdowns(content: String) =
        splitContent(content).filterIsInstance<ContentSegment.Markdown>()

    @Test fun `inline JSON blob in prose becomes a json code block`() {
        // >= 80 chars (single-line), so it crosses the conservative length threshold.
        val json = """{"results":[{"name":"alpha"},{"name":"bravo"},{"name":"charlie"},{"name":"delta"}]}"""
        val content = "Here is what I found:\n$json\nLet me know if that helps."

        val segments = splitContent(content)
        val cb = segments.filterIsInstance<ContentSegment.CodeBlock>()

        assertEquals("exactly one code block expected", 1, cb.size)
        assertEquals("json", cb[0].language)
        assertEquals(json, cb[0].code)

        val proseText = segments.filterIsInstance<ContentSegment.Markdown>()
            .joinToString("\n") { it.text }
        assertTrue(proseText.contains("Here is what I found"))
        assertTrue(proseText.contains("Let me know if that helps"))
        // The JSON is no longer present in any prose segment.
        assertTrue(!proseText.contains("\"results\""))
    }

    @Test fun `normal prose with markdown and stray braces is left untouched`() {
        val content =
            "This is **bold** text with a [link](https://example.com) and a stray {x} token. " +
                "It also mentions an array [1, 2] inline but none of that is real JSON."

        val segments = splitContent(content)

        assertTrue("no code block should be produced", codeBlocks(content).isEmpty())
        assertEquals("should be a single markdown segment", 1, segments.size)
        assertTrue(segments[0] is ContentSegment.Markdown)
        assertEquals(content.trim(), (segments[0] as ContentSegment.Markdown).text)
    }

    @Test fun `already fenced json block is not double processed`() {
        val content = "Output:\n```json\n{\"a\": 1, \"b\": [2, 3]}\n```\nDone."

        val segments = splitContent(content)
        val cb = segments.filterIsInstance<ContentSegment.CodeBlock>()

        assertEquals("exactly one code block", 1, cb.size)
        assertEquals("json", cb[0].language)
        assertEquals("{\"a\": 1, \"b\": [2, 3]}", cb[0].code)
    }

    @Test fun `brace run that does not parse stays as prose`() {
        val notJson = "{this is not json, it just starts with a brace and is over eighty chars long for sure}"
        val content = "Consider this pseudo structure:\n$notJson\nThat is just notation."

        val segments = splitContent(content)

        assertTrue("no code block for non-parsing brace run", codeBlocks(content).isEmpty())
        assertEquals("single markdown segment", 1, segments.size)
        assertTrue((segments[0] as ContentSegment.Markdown).text.contains(notJson))
    }

    @Test fun `multiple json blobs in one prose segment are each extracted`() {
        val first = """{"results":[{"id":1,"label":"first-blob-which-is-now-long-enough-to-cross-threshold"}]}"""
        val second = """{"errors":[{"code":500,"detail":"second-blob-which-is-now-long-enough-to-cross"}]}"""
        val content = "First result:\n$first\nAnd then the second one:\n$second\nThat is all."

        val segments = splitContent(content)
        val cb = segments.filterIsInstance<ContentSegment.CodeBlock>()

        assertEquals("two json code blocks expected", 2, cb.size)
        assertTrue(cb.all { it.language == "json" })
        assertEquals(first, cb[0].code)
        assertEquals(second, cb[1].code)

        val proseText = segments.filterIsInstance<ContentSegment.Markdown>()
            .joinToString("\n") { it.text }
        assertTrue(proseText.contains("First result"))
        assertTrue(proseText.contains("And then the second one"))
        assertTrue(proseText.contains("That is all"))
    }

    @Test fun `multiline json array is extracted`() {
        val json = "[\n  {\"name\": \"x\"},\n  {\"name\": \"y\"}\n]"
        val content = "Listing:\n$json\nEnd of list."

        val cb = codeBlocks(content)
        assertEquals(1, cb.size)
        assertEquals("json", cb[0].language)
        assertEquals(json, cb[0].code)
    }

    @Test fun `short inline json under threshold stays prose`() {
        val content = "A tiny object {\"a\":1} sits inline here and should stay as prose."
        assertTrue(codeBlocks(content).isEmpty())
        assertEquals(1, markdowns(content).size)
    }

    // Chat text streams in deltas: a mid-stream UNBALANCED JSON blob must stay
    // prose (no fence, no crash) until it completes and balances on a later delta.
    @Test fun `partial unbalanced json mid-stream stays prose and does not crash`() {
        val partial = "Here are the results so far:\n{\"results\": [{\"name\": \"slides_batch_upda"
        val segments = splitContent(partial)
        assertTrue(codeBlocks(partial).isEmpty())
        assertEquals(1, segments.size)
        assertTrue(segments[0] is ContentSegment.Markdown)
    }
}
