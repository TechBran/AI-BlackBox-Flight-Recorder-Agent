package com.aiblackbox.portal.util

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

    // ui_reply value out of a whole-message envelope object. Tolerant of
    // whitespace and key order; only used after we confirm a LEADING { object.
    private val UI_REPLY = Regex("""\"ui_reply\"\s*:\s*\"((?:[^\"\\]|\\.)*)\"""")

    /**
     * Produce speakable text from a raw reply. See class docs for the rules.
     */
    fun stripNonSpeakable(text: String?): String {
        if (text.isNullOrEmpty()) return ""

        var out: String = text

        // 1. Whole-message {"ui_reply":...} envelope (optionally ```json-fenced).
        //    LEADING-only: only unwrap when the envelope IS the whole message.
        run {
            var candidate = out.trim()
            WHOLE_FENCE.find(candidate)?.let { candidate = it.groupValues[1].trim() }
            if (candidate.startsWith("{") && candidate.contains("\"ui_reply\"")) {
                UI_REPLY.find(candidate)?.let { m ->
                    val raw = m.groupValues[1]
                    // Unescape the JSON string body (\" \\ \n \t \r).
                    val inner = unescapeJson(raw)
                    if (inner.isNotBlank()) out = inner
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

    private fun unescapeJson(s: String): String {
        val sb = StringBuilder(s.length)
        var i = 0
        while (i < s.length) {
            val c = s[i]
            if (c == '\\' && i + 1 < s.length) {
                when (val e = s[i + 1]) {
                    '"' -> sb.append('"')
                    '\\' -> sb.append('\\')
                    '/' -> sb.append('/')
                    'n' -> sb.append('\n')
                    't' -> sb.append('\t')
                    'r' -> sb.append('\r')
                    'b' -> sb.append('\b')
                    else -> { sb.append('\\'); sb.append(e) }
                }
                i += 2
            } else {
                sb.append(c)
                i++
            }
        }
        return sb.toString()
    }
}
