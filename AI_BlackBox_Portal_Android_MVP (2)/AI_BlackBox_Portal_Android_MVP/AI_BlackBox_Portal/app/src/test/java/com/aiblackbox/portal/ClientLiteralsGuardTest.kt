package com.aiblackbox.portal

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Grep gate over the app's production sources (audit rank 37/38, A09-data-local-model-05,
 * A14-ui-misc-14): no operator name and no pinned stale model id may ship as a string
 * literal. The operator comes from the box (GET /operators via OperatorResolver) and an
 * empty model is "Auto", the box's own default; a literal in either place tags a fresh
 * box's ledger with someone who is not there, or sends a model the box never chose.
 * Comments may still credit people by name, so only code lines are scanned.
 *
 * Pure (no Android framework) — reads the sources as text on the host JVM. The web
 * Portal has the same gate (tests/web/client-literals-guard.test.mjs).
 */
class ClientLiteralsGuardTest {

    /** `app/src/main/java`, found from whichever directory the unit tests run in. */
    private fun mainSources(): File {
        var dir: File? = File("").absoluteFile
        repeat(8) {
            val d = dir ?: return@repeat
            for (rel in listOf("src/main/java", "app/src/main/java")) {
                val f = File(d, rel)
                if (File(f, "com/aiblackbox/portal").isDirectory) return f
            }
            dir = d.parentFile
        }
        throw AssertionError("app/src/main/java not found from ${File("").absolutePath}")
    }

    private val stringLiteral = Regex("\"((?:\\\\.|[^\"\\\\\\n])*)\"")

    /** Code lines only: whole-line comments and KDoc are prose; so is a trailing `// ...`. */
    private fun codeLines(file: File): List<Pair<Int, String>> =
        file.readLines().mapIndexed { i, line ->
            val t = line.trim()
            val code = if (t.startsWith("//") || t.startsWith("*") || t.startsWith("/*")) ""
            else line.replace(Regex("\\s//\\s.*$"), "")
            i + 1 to code
        }

    /** `file:line: "literal"` for every string literal on a code line whose text matches [rx]. */
    private fun literalHits(rx: Regex): List<String> {
        val root = mainSources()
        return root.walkTopDown().filter { it.isFile && it.extension == "kt" }.flatMap { f ->
            codeLines(f).flatMap { (n, line) ->
                stringLiteral.findAll(line).filter { rx.containsMatchIn(it.groupValues[1]) }
                    .map { "${f.relativeTo(root)}:$n: ${it.value}" }.toList()
            }
        }.sorted().toList()
    }

    @Test fun `the scan reaches the store, the view models and the overlay`() {
        val root = mainSources()
        for (rel in listOf(
            "com/aiblackbox/portal/data/store/BlackBoxStore.kt",
            "com/aiblackbox/portal/ui/chat/ChatViewModel.kt",
            "com/aiblackbox/portal/overlay/OverlayService.kt",
        )) assertTrue(rel, File(root, rel).isFile)
        // And the matcher sees a literal on a code line, not one in a comment.
        assertTrue(stringLiteral.findAll("""val op = x ?: "Brandon" // seed""").any { "Brandon" in it.value })
    }

    @Test fun `no operator name is hard-coded in production code`() {
        // The owner's seed operator name is the one that leaked into every fallback.
        assertEquals(emptyList<String>(), literalHits(Regex("\\bBrandon\\b")))
    }

    @Test fun `no stale model id is pinned in production code`() {
        // Not on the live catalogs (2026-10-17). Auto (an empty model) or a live id instead;
        // ModelSelection's migration tables name them as data, and are exempt.
        val hits = literalHits(Regex("^(?:claude-sonnet-4-5|claude-haiku-4-5|grok-3-mini-beta)$"))
            .filterNot { it.startsWith("com/aiblackbox/portal/data/store/ModelSelection.kt:") }
        assertEquals(emptyList<String>(), hits)
    }
}
