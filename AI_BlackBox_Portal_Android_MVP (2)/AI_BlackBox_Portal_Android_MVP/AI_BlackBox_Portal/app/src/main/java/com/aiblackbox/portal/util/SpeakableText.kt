package com.aiblackbox.portal.util

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/**
 * Speakable-text sanitizer (plan §3.5) -- shared rules with the Portal web
 * surface (Portal/modules/tts-stt.js stripNonSpeakable).
 *
 * Auto-TTS otherwise reads [ARTIFACT:...] blocks, /ui/uploads media URLs, fenced
 * code/JSON, and leaked {"ui_reply":...} envelopes ALOUD verbatim. This strips
 * that non-speakable content BEFORE the text is sent to /tts/batch.
 *
 * Pure (String in -> String out). Order matters: unwrap a whole-message envelope
 * first, then strip artifacts/code/urls. Conservative: normal prose is untouched;
 * mid-prose JSON is never extracted (mirrors Orchestrator/reply_envelope.py's
 * LEADING-only rule).
 */
object SpeakableText {

    // Whole-string code fence wrapping the ENTIRE payload (e.g. ```json ... ```).
    private val WHOLE_FENCE =
        Regex("""^```[ \t]*[A-Za-z0-9_+-]*[ \t]*\r?\n([\s\S]*?)\r?\n?```$""")

    // [ARTIFACT:...]...[/ARTIFACT] block (DOTALL, non-greedy).
    private val ARTIFACT_BLOCK = Regex("""\[ARTIFACT:[\s\S]*?\[/ARTIFACT]""")

    // A lone, unclosed [ARTIFACT:...] opener.
    private val ARTIFACT_OPENER = Regex("""\[ARTIFACT:[^]]*]""")

    // Fenced code/JSON block.
    private val FENCED_CODE = Regex("""```[\s\S]*?```""")

    // Bare media URLs: absolute .../ui/uploads/... and relative /ui/uploads/...
    private val ABS_MEDIA_URL = Regex("""https?://[^\s)]*/ui/uploads/\S+""")
    private val REL_MEDIA_URL = Regex("""/ui/uploads/\S+""")

    private val WHITESPACE = Regex("""\s+""")

    // Strict JSON parser (NOT lenient) used to validate a whole-message
    // envelope. Mirrors Portal's JSON.parse / reply_envelope.py's json.loads:
    // only a string that ACTUALLY parses as JSON is unwrapped. kotlinx is used
    // (NOT org.json) because org.json is stubbed to no-op defaults under
    // testUnitTests.returnDefaultValues=true, which would make the parse check
    // a no-op in unit tests (see MarkdownText.kt's STRICT_JSON for precedent).
    private val STRICT_JSON = Json { isLenient = false }

    /**
     * Produce speakable text from a raw reply. See class docs for the rules.
     */
    fun stripNonSpeakable(text: String?): String {
        if (text.isNullOrEmpty()) return ""

        var out: String = text

        // 1. Whole-message {"ui_reply":...} envelope (optionally ```json-fenced).
        //    LEADING-only AND JSON-VALIDATED: only unwrap when the LEADING object
        //    actually parses as JSON and carries a string "ui_reply". This mirrors
        //    Portal (JSON.parse) and reply_envelope.py (json.loads) so the same
        //    reply is spoken identically on every surface. Malformed-but-leading
        //    input (trailing comma, fake ui_reply in non-JSON, JSON5) is PRESERVED.
        run {
            var candidate = out.trim()
            WHOLE_FENCE.find(candidate)?.let { candidate = it.groupValues[1].trim() }
            if (candidate.startsWith("{")) {
                try {
                    val parsed = STRICT_JSON.parseToJsonElement(candidate)
                    if (parsed is JsonObject) {
                        val v = parsed["ui_reply"]
                        if (v is JsonPrimitive && v.isString) {
                            val inner = v.content
                            if (inner.isNotBlank()) out = inner
                        }
                    }
                } catch (_: Exception) {
                    // Not a parseable whole-message envelope -> leave text as-is.
                }
            }
        }

        // 2. Remove [ARTIFACT:...]...[/ARTIFACT] blocks, then a lone opener.
        out = ARTIFACT_BLOCK.replace(out, " ")
        out = ARTIFACT_OPENER.replace(out, " ")

        // 3. Fenced code/JSON -> the words "code block".
        out = FENCED_CODE.replace(out, " code block ")

        // 4. Bare media URLs -> removed.
        out = ABS_MEDIA_URL.replace(out, " ")
        out = REL_MEDIA_URL.replace(out, " ")

        // 5. Collapse leftover whitespace.
        out = WHITESPACE.replace(out, " ").trim()

        return out
    }
}
