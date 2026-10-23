package com.aiblackbox.portal

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Grep gate over the app's production sources (audit rank 37/38, A09-data-local-model-05,
 * A14-ui-misc-14): no operator name and no pinned stale model id may ship. The operator
 * comes from the box (GET /operators via OperatorResolver) and an empty model is "Auto",
 * the box's own default; a name or id in either place tags a fresh box's ledger with
 * someone who is not there, or sends a model the box never chose.
 *
 * The scan reads the code with comments blanked (a small lexer that knows strings, raw
 * strings, char literals and nested block comments), so string literals of any shape,
 * identifiers and multi-line raw strings are all seen, in any case, while comments may
 * still credit people by name. Resources and assets (src/main/res, src/main/assets, the
 * manifest) are scanned too, XML comments blanked. ModelSelection's migration tables name
 * the stale ids as data and are the only exemption.
 *
 * Pure (no Android framework) — reads the sources as text on the host JVM. The web
 * Portal has the same gate (tests/web/client-literals-guard.test.mjs).
 */
class ClientLiteralsGuardTest {

    /** `app/src/main`, found from whichever directory the unit tests run in. */
    private fun mainDir(): File {
        var dir: File? = File("").absoluteFile
        repeat(8) {
            val d = dir ?: return@repeat
            for (rel in listOf("src/main", "app/src/main")) {
                val f = File(d, rel)
                if (File(f, "java/com/aiblackbox/portal").isDirectory) return f
            }
            dir = d.parentFile
        }
        throw AssertionError("app/src/main not found from ${File("").absolutePath}")
    }

    private companion object {
        // The owner's seed operator name is the one that leaked into every fallback.
        val OPERATOR_NAME = Regex("brandon", RegexOption.IGNORE_CASE)

        // Not on the live catalogs (2026-10-17): the undated 4.5 aliases and the retired
        // Grok 3 Mini beta. A dated live id (claude-sonnet-4-5-20250929) is not one of them.
        val STALE_MODEL_ID = Regex("(?<![\\w.-])(?:claude-sonnet-4-5|claude-haiku-4-5|grok-3-mini-beta)(?![\\w.-])")

        /** ModelSelection's migration tables: they name the stale ids as what to rewrite. */
        val MIGRATION_TABLE = Regex("val (?:RETIRED_MODEL_IDS|DROPPED_PICKER_IDS|DROPPED_NON_CLAUDE_IDS)\\b[^=\\n]*=\\s*(?:mapOf|setOf)\\(")

        const val MODEL_SELECTION = "java/com/aiblackbox/portal/data/store/ModelSelection.kt"

        val TEXT_RESOURCE = setOf("xml", "json", "txt", "html", "js", "properties")

        /** A comment's text blanked, its line breaks kept (so line numbers still match). */
        fun blank(s: CharSequence): String = s.toString().replace(Regex("[^\\n]"), "")

        /**
         * Kotlin/Java source with every comment blanked (line breaks kept). Strings, raw
         * strings and char literals are copied as they are, so a comment opener inside one
         * is never taken for a comment, and a quote in a char literal never opens a string.
         */
        fun stripComments(src: String): String {
            val out = StringBuilder(src.length)
            var i = 0
            while (i < src.length) {
                val c = src[i]
                val next = src.getOrNull(i + 1)
                when {
                    src.startsWith("\"\"\"", i) -> {
                        var end = src.indexOf("\"\"\"", i + 3).let { if (it < 0) src.length else it + 3 }
                        while (end < src.length && src[end] == '"') end++  // """a""""
                        out.append(src, i, end); i = end
                    }
                    c == '"' || c == '\'' -> {
                        var j = i + 1
                        while (j < src.length && src[j] != c && src[j] != '\n') { if (src[j] == '\\') j++; j++ }
                        j = minOf(j + 1, src.length)
                        out.append(src, i, j); i = j
                    }
                    c == '/' && next == '/' -> {
                        while (i < src.length && src[i] != '\n') i++
                    }
                    c == '/' && next == '*' -> {
                        var depth = 0
                        var j = i
                        do {
                            when {
                                src.startsWith("/*", j) -> { depth++; j += 2 }
                                src.startsWith("*/", j) -> { depth--; j += 2 }
                                else -> { if (src[j] == '\n') out.append('\n'); j++ }
                            }
                        } while (depth > 0 && j < src.length)
                        i = j
                    }
                    else -> { out.append(c); i++ }
                }
            }
            return out.toString()
        }

        /** [code] (comments already blanked) with ModelSelection's migration tables blanked. */
        fun blankMigrationTables(code: String): String {
            var text = code
            for (m in MIGRATION_TABLE.findAll(code).toList().asReversed()) {
                var depth = 0
                var j = m.range.last  // the table's "("
                var quote: Char? = null
                while (j < text.length) {
                    val ch = text[j]
                    if (quote != null) {
                        if (ch == '\\') j++ else if (ch == quote) quote = null
                    } else if (ch == '"') {
                        quote = ch
                    } else if (ch == '(') {
                        depth++
                    } else if (ch == ')' && --depth == 0) {
                        break
                    }
                    j++
                }
                val end = minOf(j + 1, text.length)
                text = text.substring(0, m.range.first) + blank(text.substring(m.range.first, end)) + text.substring(end)
            }
            return text
        }

        /** The file at [path] (relative to src/main) as it ships, comments blanked. */
        fun codeText(path: String, src: String): String = when {
            path.endsWith(".kt") || path.endsWith(".java") ->
                stripComments(src).let { if (path == MODEL_SELECTION) blankMigrationTables(it) else it }
            path.endsWith(".xml") -> src.replace(Regex("<!--[\\s\\S]*?-->")) { blank(it.value) }
            else -> src
        }

        /** 1-based numbers of the lines of [src] whose code matches [rx]. */
        fun matchingLines(path: String, src: String, rx: Regex): List<Int> =
            codeText(path, src).split('\n').withIndex().filter { rx.containsMatchIn(it.value) }.map { it.index + 1 }
    }

    /** Every production text file under src/main, keyed by its path relative to it. */
    private fun productionFiles(): Map<String, File> {
        val main = mainDir()
        return main.walkTopDown().filter { f ->
            f.isFile && (f.extension in setOf("kt", "java") || f.extension in TEXT_RESOURCE)
        }.associateBy { it.relativeTo(main).invariantSeparatorsPath }.toSortedMap()
    }

    /** `path:line: code` for every production line whose code matches [rx]. */
    private fun hits(rx: Regex): List<String> = productionFiles().flatMap { (path, file) ->
        val src = file.readText()
        val lines = src.split('\n')
        matchingLines(path, src, rx).map { n -> "$path:$n: ${lines[n - 1].trim().take(140)}" }
    }

    @Test fun `the scan reaches the store, the view models, the overlay and the resources`() {
        val files = productionFiles().keys
        for (rel in listOf(
            "java/com/aiblackbox/portal/data/store/BlackBoxStore.kt",
            "java/com/aiblackbox/portal/ui/chat/ChatViewModel.kt",
            "java/com/aiblackbox/portal/overlay/OverlayService.kt",
            MODEL_SELECTION,
            "AndroidManifest.xml",
            "res/values/strings.xml",
        )) assertTrue(rel, rel in files)
    }

    @Test fun `no operator name is hard-coded in production code`() {
        assertEquals(emptyList<String>(), hits(OPERATOR_NAME))
    }

    @Test fun `no stale model id is pinned in production code`() {
        assertEquals(emptyList<String>(), hits(STALE_MODEL_ID))
    }

    // ── the gate's own reach (planted samples, never production files) ───────

    @Test fun `the gate sees the name in any string, identifier or resource`() {
        val kt = listOf(
            """val op = stored.ifBlank { "Brandon" }""",          // 1 literal
            "val prompt = \"\"\"",                                // 2 raw string ...
            "    Hello,",                                         // 3
            "    brandon",                                        // 4 ... the name alone, lower case
            "\"\"\".trimIndent()",                                // 5
            """val u = "x // y" + "Brandon"""",                   // 6 a // inside a string
            "val brandonDefault = 1",                             // 7 an identifier
            """val c = '"'; val d = "Brandon__claude"""",         // 8 a quote in a char literal
        ).joinToString("\n")
        assertEquals(listOf(1, 4, 6, 7, 8), matchingLines("java/X.kt", kt, OPERATOR_NAME))
        val xml = "<resources>\n  <string name=\"who\">Brandon</string>\n  <!-- Brandon's layout -->\n</resources>"
        assertEquals(listOf(2), matchingLines("res/values/x.xml", xml, OPERATOR_NAME))
    }

    @Test fun `the gate leaves comments alone, so people can still be credited`() {
        val kt = listOf(
            "// Brandon asked for this (2026-07-24)",
            "/* Brandon's /* nested */ fit pass */ val a = 1",
            "val b = 2 // per Brandon",
            "/**",
            " * Brandon: keep the confirm dialog.",
            " */",
            """val url = "https://example.test/x" // Brandon""",
        ).joinToString("\n")
        assertEquals(emptyList<Int>(), matchingLines("java/X.kt", kt, OPERATOR_NAME))
    }

    @Test fun `only ModelSelection's migration tables may name a stale id`() {
        val kt = listOf(
            "val RETIRED_MODEL_IDS: Map<String, String> = mapOf(",   // 1
            "    \"claude-sonnet-4-5\" to \"claude-sonnet-5-5\",",   // 2 exempt: the table
            ")",                                                   // 3
            "val DROPPED_NON_CLAUDE_IDS: Set<String> = setOf(\"grok-3-mini-beta\")",  // 4 exempt
            "val pinned = \"claude-haiku-4-5\"",                    // 5 not a table
            "val live = \"claude-sonnet-4-5-20250929\"",           // 6 a live dated id
        ).joinToString("\n")
        assertEquals(listOf(5), matchingLines(MODEL_SELECTION, kt, STALE_MODEL_ID))
        assertEquals(listOf(2, 4, 5), matchingLines("java/Other.kt", kt, STALE_MODEL_ID))
    }
}
