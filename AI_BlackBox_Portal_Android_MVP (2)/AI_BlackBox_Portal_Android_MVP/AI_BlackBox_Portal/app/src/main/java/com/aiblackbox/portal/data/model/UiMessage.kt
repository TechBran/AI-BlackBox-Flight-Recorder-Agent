package com.aiblackbox.portal.data.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import java.util.UUID

@Serializable
data class UiMessage(
    val id: String = UUID.randomUUID().toString(),
    val role: String,                    // "user" or "assistant"
    val content: String,                 // rendered text content
    val reasoning: String? = null,       // thinking/reasoning (expandable)
    val timestamp: Long = System.currentTimeMillis(),
    val isStreaming: Boolean = false,     // currently being streamed
    val isThinking: Boolean = false,     // currently in thinking phase
    val model: String? = null,
    val provider: String? = null,
    val images: List<String> = emptyList(),       // attached image URLs
    val attachments: List<String> = emptyList(),  // other file URLs
    val tokens: TokenCount? = null,
    val mediaTasks: List<String> = emptyList(),   // pending image/video/music task IDs
    val artifacts: List<ArtifactRef> = emptyList(), // downloadable artifacts from /chat/save (Phase 6b)
    val provenance: Provenance? = null,            // typed retrieval provenance (recent/keyword/semantic/checkpoint)
    val ttsAudioUrl: String? = null,              // URL of generated TTS audio for inline player
    val ttsGenerating: Boolean = false,            // true while TTS is being generated
    val sendFailed: Boolean = false                // user turn whose send failed with nothing usable arrived — UI offers retry (default keeps old persisted history loading)
)

/**
 * A downloadable artifact returned by /chat/save (Phase 6a backend). Rendered as a
 * native download chip below the assistant bubble (Phase 6b). The file is served at
 * {baseUrl}{url} (e.g. http://host:9091/artifacts/<id>).
 */
@Serializable
data class ArtifactRef(
    val filename: String,
    val type: String,
    val url: String,                 // "/artifacts/<id>" — resolve against baseUrl
    @SerialName("size_kb") val sizeKb: Double = 0.0
)
