package com.aiblackbox.portal

import com.aiblackbox.portal.ui.components.TaskUi
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * TaskUi is the Kotlin mirror of Portal/modules/task-ui.js (G3-T12/T13). These
 * tests pin the mirror to the JS spec so the two task surfaces cannot drift:
 * every type resolves to the right icon+label, the CU/CLI predicates gate the
 * right pills, canShowLiveView is null/empty-safe, and truncateText matches.
 */
class TaskUiTest {

    // ── taskTypeMeta: canonical enum values ──────────────────────────────────

    @Test fun `media and chat canonical types resolve`() {
        assertEquals(TaskUi.TypeMeta("🎨", "Image Generation"), TaskUi.taskTypeMeta("image_generation"))
        assertEquals(TaskUi.TypeMeta("🎬", "Video Generation"), TaskUi.taskTypeMeta("video_generation"))
        assertEquals(TaskUi.TypeMeta("🎧", "Audio Analysis"), TaskUi.taskTypeMeta("audio_analysis"))
        assertEquals(TaskUi.TypeMeta("🔊", "Text-to-Speech"), TaskUi.taskTypeMeta("google_tts"))
        assertEquals(TaskUi.TypeMeta("🔊", "Text-to-Speech"), TaskUi.taskTypeMeta("gemini_tts"))
        assertEquals(TaskUi.TypeMeta("🎵", "Music Generation"), TaskUi.taskTypeMeta("lyria_music"))
        assertEquals(TaskUi.TypeMeta("🎵", "Music Generation"), TaskUi.taskTypeMeta("elevenlabs_music"))
        assertEquals(TaskUi.TypeMeta("💬", "Chat Response"), TaskUi.taskTypeMeta("chat"))
        assertEquals(TaskUi.TypeMeta("💬", "Agent Chat"), TaskUi.taskTypeMeta("agent_chat"))
        assertEquals(TaskUi.TypeMeta("💾", "Checkpoint"), TaskUi.taskTypeMeta("checkpoint"))
    }

    @Test fun `computer use types resolve to the CU icon and label`() {
        assertEquals(TaskUi.TypeMeta("💻", "Computer Use"), TaskUi.taskTypeMeta("use_computer"))
        assertEquals(TaskUi.TypeMeta("💻", "Computer Use"), TaskUi.taskTypeMeta("browser_use"))
        assertEquals(TaskUi.TypeMeta("💻", "Computer Use"), TaskUi.taskTypeMeta("gemini_cu"))
    }

    @Test fun `cli agent types resolve to keyboard icon and product labels`() {
        assertEquals(TaskUi.TypeMeta("⌨️", "CLI Agent"), TaskUi.taskTypeMeta("cli_agent"))
        assertEquals(TaskUi.TypeMeta("⌨️", "Claude Code"), TaskUi.taskTypeMeta("claude_code_task"))
        assertEquals(TaskUi.TypeMeta("⌨️", "Gemini CLI"), TaskUi.taskTypeMeta("gemini_cli_task"))
        assertEquals(TaskUi.TypeMeta("⌨️", "Codex"), TaskUi.taskTypeMeta("codex_cli_task"))
    }

    @Test fun `legacy short keys resolve`() {
        assertEquals(TaskUi.TypeMeta("🎨", "Image Generation"), TaskUi.taskTypeMeta("image"))
        assertEquals(TaskUi.TypeMeta("🎬", "Video Generation"), TaskUi.taskTypeMeta("video"))
        assertEquals(TaskUi.TypeMeta("🎧", "Audio Analysis"), TaskUi.taskTypeMeta("audio"))
        assertEquals(TaskUi.TypeMeta("🔊", "Text-to-Speech"), TaskUi.taskTypeMeta("tts"))
        assertEquals(TaskUi.TypeMeta("🎙️", "SSML Generation"), TaskUi.taskTypeMeta("ssml"))
    }

    // ── taskTypeMeta: cli_agent provider resolution ──────────────────────────

    @Test fun `cli_agent with known provider resolves to product label keeping the icon`() {
        assertEquals(TaskUi.TypeMeta("⌨️", "Claude Code"), TaskUi.taskTypeMeta("cli_agent", "claude"))
        assertEquals(TaskUi.TypeMeta("⌨️", "Gemini CLI"), TaskUi.taskTypeMeta("cli_agent", "gemini"))
        assertEquals(TaskUi.TypeMeta("⌨️", "Codex"), TaskUi.taskTypeMeta("cli_agent", "codex"))
    }

    @Test fun `cli_agent with unknown or missing provider falls back to CLI Agent`() {
        assertEquals(TaskUi.TypeMeta("⌨️", "CLI Agent"), TaskUi.taskTypeMeta("cli_agent", null))
        assertEquals(TaskUi.TypeMeta("⌨️", "CLI Agent"), TaskUi.taskTypeMeta("cli_agent", "unknown"))
    }

    @Test fun `provider only applies to cli_agent`() {
        // A CU task ignores any provider hint.
        assertEquals(TaskUi.TypeMeta("💻", "Computer Use"), TaskUi.taskTypeMeta("use_computer", "claude"))
    }

    // ── taskTypeMeta: unknown / null / empty never blank ─────────────────────

    @Test fun `unknown type falls back to gear plus raw type`() {
        assertEquals(TaskUi.TypeMeta("⚙️", "some_new_type"), TaskUi.taskTypeMeta("some_new_type"))
    }

    @Test fun `null type falls back to gear plus Task`() {
        assertEquals(TaskUi.TypeMeta("⚙️", "Task"), TaskUi.taskTypeMeta(null))
    }

    @Test fun `empty type falls back to gear plus Task`() {
        assertEquals(TaskUi.TypeMeta("⚙️", "Task"), TaskUi.taskTypeMeta(""))
    }

    // ── predicates ───────────────────────────────────────────────────────────

    @Test fun `isCUTaskType`() {
        assertTrue(TaskUi.isCUTaskType("use_computer"))
        assertTrue(TaskUi.isCUTaskType("browser_use"))
        assertTrue(TaskUi.isCUTaskType("gemini_cu"))
        assertFalse(TaskUi.isCUTaskType("cli_agent"))
        assertFalse(TaskUi.isCUTaskType("image_generation"))
        assertFalse(TaskUi.isCUTaskType(null))
    }

    @Test fun `isCLITaskType`() {
        assertTrue(TaskUi.isCLITaskType("cli_agent"))
        assertTrue(TaskUi.isCLITaskType("claude_code_task"))
        assertTrue(TaskUi.isCLITaskType("gemini_cli_task"))
        assertTrue(TaskUi.isCLITaskType("codex_cli_task"))
        assertFalse(TaskUi.isCLITaskType("use_computer"))
        assertFalse(TaskUi.isCLITaskType(null))
    }

    @Test fun `isAgentTaskType is CU union CLI`() {
        assertTrue(TaskUi.isAgentTaskType("use_computer"))
        assertTrue(TaskUi.isAgentTaskType("cli_agent"))
        assertFalse(TaskUi.isAgentTaskType("image_generation"))
        assertFalse(TaskUi.isAgentTaskType(null))
    }

    // ── canShowLiveView (null / empty safe) ──────────────────────────────────

    @Test fun `canShowLiveView requires CU type and a non-empty device`() {
        assertTrue(TaskUi.canShowLiveView("use_computer", "blackbox"))
        assertTrue(TaskUi.canShowLiveView("gemini_cu", "phone-1"))
        assertFalse(TaskUi.canShowLiveView("use_computer", null))
        assertFalse(TaskUi.canShowLiveView("use_computer", ""))
        assertFalse(TaskUi.canShowLiveView("cli_agent", "blackbox")) // not CU
        assertFalse(TaskUi.canShowLiveView(null, "blackbox"))
        assertFalse(TaskUi.canShowLiveView(null, null))
    }

    // ── truncateText ─────────────────────────────────────────────────────────

    @Test fun `truncateText returns empty for null blank and whitespace`() {
        assertEquals("", TaskUi.truncateText(null))
        assertEquals("", TaskUi.truncateText(""))
        assertEquals("", TaskUi.truncateText("   \n\t  "))
    }

    @Test fun `truncateText collapses runs of whitespace to single spaces`() {
        assertEquals("a b c", TaskUi.truncateText("a\n\nb  c"))
        assertEquals("hello world", TaskUi.truncateText("  hello   world  "))
    }

    @Test fun `truncateText leaves short strings unchanged`() {
        assertEquals("short line", TaskUi.truncateText("short line", 140))
    }

    @Test fun `truncateText at exactly max is unchanged`() {
        assertEquals("abcde", TaskUi.truncateText("abcde", 5))
    }

    @Test fun `truncateText over max truncates with an ellipsis`() {
        // 8 chars, max 5 → 4 chars + ellipsis, total 5 visible units.
        assertEquals("abcd…", TaskUi.truncateText("abcdefgh", 5))
    }

    @Test fun `truncateText default max is 140`() {
        val long = "x".repeat(200)
        val out = TaskUi.truncateText(long)
        assertEquals(140, out.length)
        assertTrue(out.endsWith("…"))
    }
}
