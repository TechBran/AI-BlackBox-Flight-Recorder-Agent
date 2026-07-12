package com.aiblackbox.portal.data.voice

/** P3.17: any role beyond user/assistant renders as a compact chip. */
fun isChipRole(role: String): Boolean = role != "user" && role != "assistant"

/** Compact chip label for tool activity. Pure — no android.util.Log. */
fun toolChipText(kind: String, name: String, detail: String): String {
    val suffix = detail.take(80).let { if (it.isBlank()) "" else " — $it" }
    return when (kind) {
        "tool_call" -> "🔧 ${name.ifBlank { "tool" }}$suffix"
        "tool_result" -> "✔ ${name.ifBlank { "tool" }}$suffix"
        "image_task" -> "🖼 image task$suffix"
        "video_task" -> "🎬 video task$suffix"
        "music_task" -> "🎵 music task$suffix"
        else -> "$kind$suffix"
    }
}

/** Merge the server transcript with locally-injected entries (chips, typed text)
 *  in timestamp order. sortedBy is stable: equal stamps keep server-before-local. */
fun mergeTranscript(server: List<TranscriptEntry>, local: List<TranscriptEntry>): List<TranscriptEntry> =
    (server + local).sortedBy { it.timestamp }
