package com.aiblackbox.portal.data.repository

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

/**
 * WIRING guard: one poller for box tasks (audit A11-settings-generation-18, rank 76).
 * Only TaskRepository reads GET /tasks/status/{id}, and only TaskPoller polls it, so a new
 * screen cannot grow a sixth poll loop with its own terminal set and URL handling.
 */
class TaskPollerWiringTest {

    private val root: File by lazy {
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            for (c in listOf(File(dir, "src/main/java"), File(dir, "app/src/main/java"))) {
                if (c.isDirectory) return@lazy File(c, "com/aiblackbox/portal")
            }
            dir = dir.parentFile
        }
        fail("could not locate src/main/java")
        error("unreachable")
    }

    private fun code(file: File): List<String> = file.readLines().filter { line ->
        val t = line.trim()
        !t.startsWith("//") && !t.startsWith("*") && !t.startsWith("/*")
    }

    @Test fun `only the repository reads a task's status`() {
        val readers = root.walkTopDown()
            .filter { f -> f.isFile && f.extension == "kt" && code(f).any { "/tasks/status/" in it || "/tasks/\$taskId\"" in it } }
            .map { it.relativeTo(root).invariantSeparatorsPath }
            .toSortedSet()
        assertEquals(sortedSetOf("data/repository/TaskRepository.kt"), readers)
    }

    @Test fun `every task wait goes through TaskPoller`() {
        for (rel in listOf(
            "ui/generation/GenerationViewModel.kt",
            "ui/generation/GoogleSsmlScreen.kt",
            "ui/generation/GeminiProTtsScreen.kt",
            "data/repository/TtsRepository.kt",
            "ui/chat/ChatViewModel.kt",
        )) {
            assertTrue(rel, code(File(root, rel)).any { "TaskPoller(" in it })
        }
        val discovery = code(File(root, "ui/chat/TaskDiscovery.kt")).joinToString("\n")
        assertTrue(".poll(taskId" in discovery)
        assertTrue(".await(taskId" in discovery)
    }
}
