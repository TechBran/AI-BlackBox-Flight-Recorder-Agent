package com.aiblackbox.portal.ui.components

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateContentSize
import android.view.HapticFeedbackConstants
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import com.aiblackbox.portal.ui.feedback.clickFeedback
import com.aiblackbox.portal.ui.feedback.rememberPressFeedback
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AssistChip
import androidx.compose.material3.AssistChipDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.boundsInWindow
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import coil.compose.AsyncImage
import com.aiblackbox.portal.data.model.ArtifactRef
import com.aiblackbox.portal.data.model.UiMessage
import com.aiblackbox.portal.util.SpeakableText
import com.aiblackbox.portal.ui.theme.AssistantBubbleShape
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxBlack
import com.aiblackbox.portal.ui.theme.BubbleAssistantBg
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.HighlightKeyword
import com.aiblackbox.portal.ui.theme.Neutral200
import com.aiblackbox.portal.ui.theme.Neutral250
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.Neutral600
import com.aiblackbox.portal.ui.theme.RadiusMd
import com.aiblackbox.portal.ui.theme.SolidGreen
import com.aiblackbox.portal.ui.theme.RadiusLg
import com.aiblackbox.portal.ui.theme.UserBubbleShape
import kotlinx.coroutines.delay

// =============================================================================
// ChatBubble — aligned with Portal .bubble, .bubble-controls, .bubble-btn
//
// Portal bubble:
//   padding: 12px 16px, border-radius: --radius-xl, box-shadow: --shadow-xs
//   font-size: 15px, line-height: 1.6, gap: 8px
//   Controls: border-top separator, 34px glass buttons (copy, speak)
//   Timestamp: 10px, neutral-600
//
// User bubble: --bubble-user (#2C2C2C), asymmetric (sharp top-end)
// Assistant bubble: --bubble (#000000), asymmetric (sharp top-start), full width
// =============================================================================

enum class LiveTextSection { REASONING, ANSWER, TOOL_FALLBACK, COMPLETED_RETURN }
internal const val LIVE_REASONING_EDGE_TAG = "live-stream-edge-reasoning"
internal const val LIVE_ANSWER_EDGE_TAG = "live-stream-edge-answer"
internal const val LIVE_TOOL_FALLBACK_EDGE_TAG = "live-stream-edge-tool-fallback"
internal const val COMPLETED_RETURN_EDGE_TAG = "live-stream-edge-completed-return"

@OptIn(ExperimentalFoundationApi::class)
@Composable
fun ChatBubble(
    message: UiMessage,
    onSpeak: (String) -> Unit = {},
    onSpeakWithId: (String, String) -> Unit = { _, _ -> },
    onSnapshotClick: ((String) -> Unit)? = null,
    onRetry: (String) -> Unit = {},
    // "The Signal" — presentation-only telemetry label for the live turn
    // (ChatViewModel._signalLabel). EPHEMERAL: it is never part of [message] and
    // never persisted; only the SignalLine HUD (top of the column) reads it.
    signalLabel: String? = null,
    modifier: Modifier = Modifier,
    onLiveEdgePositioned: ((LiveTextSection, Float) -> Unit)? = null,
    useToolFallbackAnchor: Boolean = false,
    useCompletedReturnAnchor: Boolean = false,
) {
    val isUser = message.role == "user"
    val view = LocalView.current
    val clipboardManager = LocalClipboardManager.current
    val screenWidth = LocalConfiguration.current.screenWidthDp.dp
    val maxUserBubbleWidth = screenWidth * 0.85f

    // Copy confirmed state
    var isCopied by remember { mutableStateOf(false) }
    LaunchedEffect(isCopied) {
        if (isCopied) {
            delay(1500)
            isCopied = false
        }
    }

    // Long-press-to-copy (any bubble) — invoked by the combinedClickable below
    // (pointer long-press with haptic, and the TalkBack long-click action it
    // exposes via onLongClickLabel).
    val copyMessage = {
        clipboardManager.setText(AnnotatedString(message.content))
        isCopied = true
    }
    val bubbleInteractions = remember { MutableInteractionSource() }

    Row(
        modifier = modifier
            .fillMaxWidth()
            .padding(
                start = if (isUser) 48.dp else 4.dp,
                end = if (isUser) 4.dp else 4.dp,
                top = 4.dp,
                bottom = 4.dp
            ),
        horizontalArrangement = if (isUser) Arrangement.End else Arrangement.Start
    ) {
        // Outer layout column: bubble + (for a failed user send) the retry chip
        // UNDER the bubble. Pure layout — carries NO pointer handlers, so it can't
        // reintroduce the touch-blocking history noted on the content column below.
        Column(
            modifier = Modifier
                .then(
                if (isUser) Modifier.widthIn(max = maxUserBubbleWidth)
                else Modifier.fillMaxWidth()
                )
                .then(
                    if (useToolFallbackAnchor && !isUser) Modifier
                        .testTag(LIVE_TOOL_FALLBACK_EDGE_TAG)
                        .onGloballyPositioned { coordinates ->
                            onLiveEdgePositioned?.invoke(
                                LiveTextSection.TOOL_FALLBACK,
                                coordinates.boundsInWindow().bottom,
                            )
                        }
                    else Modifier
                )
                .then(
                    if (useCompletedReturnAnchor && !isUser) Modifier
                        .testTag(COMPLETED_RETURN_EDGE_TAG)
                        .onGloballyPositioned { coordinates ->
                            onLiveEdgePositioned?.invoke(
                                LiveTextSection.COMPLETED_RETURN,
                                coordinates.boundsInWindow().bottom,
                            )
                        }
                    else Modifier
                ),
            horizontalAlignment = if (isUser) Alignment.End else Alignment.Start
        ) {
        Column(
            modifier = Modifier
                .then(
                    if (isUser) Modifier
                    else Modifier.fillMaxWidth()
                )
                // Long-press anywhere on the bubble copies its text. SAFE pattern
                // (CliAgentEmptyState precedent): combinedClickable is the SOLE
                // clickable on the bubble background — NOT layered over another
                // clickable — so child clickables (TTS/copy buttons, media cards)
                // still win hit-testing inside their own bounds. onClick is a
                // required no-op; indication=null so plain taps don't ripple.
                // a11y: onLongClickLabel has combinedClickable expose the
                // long-press action to TalkBack under this label.
                .then(
                    if (message.content.isNotBlank()) Modifier
                        .combinedClickable(
                            interactionSource = bubbleInteractions,
                            indication = null,
                            onClick = {},
                            onLongClickLabel = "Copy message",
                            onLongClick = {
                                view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                                copyMessage()
                            },
                        )
                    else Modifier
                )
                // Flat solid fill - no shadow rim (HD/production look)
                // Use background(shape) instead of clip+background — applies visual rounding
                // WITHOUT clipping the touch area of child composables
                .background(
                    // Assistant bubble is transparent so the generation ember backdrop
                    // glows THROUGH the text (matches web; identical to black when idle).
                    color = if (isUser) Neutral250 else BubbleAssistantBg,
                    shape = if (isUser) UserBubbleShape else AssistantBubbleShape
                )
                // Matches Portal: padding 12px 16px
                .padding(horizontal = 16.dp, vertical = 12.dp),
            // Matches Portal: gap: 8px
            verticalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            // Content column — still NO pointerInput here: a raw pointerInput
            // long-press on THIS column once caused persistent touch blocking on
            // child clickables (TTS button, copy button, AudioPlayerBar play
            // button) and was removed. Long-press-to-copy now exists again, but
            // SAFELY — as the combinedClickable on the bubble background Column
            // above, which loses hit-testing to child clickables in their bounds.
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            // ── Thinking/reasoning section (collapsible) ──
            if (!message.reasoning.isNullOrBlank()) {
                var showThinking by remember(message.isThinking) {
                    mutableStateOf(message.isThinking)
                }

                Row(
                    modifier = Modifier
                        .clickFeedback { showThinking = !showThinking },
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text(
                        text = if (showThinking) "\u25BC" else "\u25B6",
                        style = MaterialTheme.typography.labelSmall,
                        color = Neutral500
                    )
                    Text(
                        text = if (message.isThinking) " Thinking..." else " Thinking",
                        style = MaterialTheme.typography.labelSmall,
                        color = if (message.isThinking) BbxAccent else Neutral500
                    )
                }
                AnimatedVisibility(visible = showThinking) {
                    Text(
                        text = message.reasoning!!,
                        modifier = Modifier
                            .then(if (message.isThinking) Modifier.testTag(LIVE_REASONING_EDGE_TAG) else Modifier)
                            .onGloballyPositioned { coordinates ->
                            if (message.isThinking) {
                                onLiveEdgePositioned?.invoke(
                                    LiveTextSection.REASONING,
                                    coordinates.boundsInWindow().bottom,
                                )
                            }
                        },
                        style = MaterialTheme.typography.bodySmall,
                        color = BbxDim
                    )
                }
            }

            // ── "The Signal" — presentation-only telemetry HUD ──
            // The Signal line is no longer rendered here — per Brandon's feedback it
            // is now pinned to the BOTTOM of the screen (a fixed strip above the
            // composer) in ChatScreen. [signalLabel] is retained for compatibility.

            // ── Image attachments ──
            if (message.images.isNotEmpty()) {
                message.images.distinctBy { it.substringBefore('?') }.forEach { url ->
                    AsyncImage(
                        model = url,
                        contentDescription = "Attached image",
                        modifier = Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(RadiusMd))
                            .padding(bottom = 4.dp),
                        contentScale = ContentScale.FillWidth
                    )
                }
            }

            // ── Media generation placeholders ──
            if (message.mediaTasks.isNotEmpty()) {
                message.mediaTasks.forEach { taskEntry ->
                    // Parse type prefix (e.g., "image:task-id-123")
                    val taskType = when {
                        taskEntry.startsWith("image:") -> "image"
                        taskEntry.startsWith("video:") -> "video"
                        taskEntry.startsWith("music:") -> "music"
                        else -> "image"
                    }
                    MediaGeneratingPlaceholder(
                        taskType = taskType,
                        modifier = Modifier.padding(bottom = 8.dp)
                    )
                }
            }

            // ── Main content ──
            if (message.content.isNotBlank()) {
                if (isUser) {
                    Text(
                        text = message.content,
                        style = MaterialTheme.typography.bodyLarge.copy(
                            lineHeight = 24.sp
                        ),
                        color = BbxWhite
                    )
                } else {
                    // Strip raw [ARTIFACT:...] blocks BEFORE rendering — once artifacts
                    // are surfaced as native download chips (below), the bracket text must
                    // never reach the bubble. Shared rule with the TTS sanitizer.
                    val displayContent = SpeakableText.stripArtifactBlocks(message.content)
                    // Extract inline media URLs from content, render them, strip from text
                    val renderedImages = message.images.map { it.substringBefore('?') }.toSet()
                    val (cleanContent, inlineMedia) = extractInlineMedia(displayContent, renderedImages)

                    // Render extracted media inline
                    inlineMedia.forEach { media ->
                        when (media.type) {
                            "image" -> {
                                AsyncImage(
                                    model = media.url,
                                    contentDescription = "Generated image",
                                    modifier = Modifier
                                        .fillMaxWidth()
                                        .clip(RoundedCornerShape(RadiusMd))
                                        .padding(bottom = 6.dp),
                                    contentScale = ContentScale.FillWidth
                                )
                            }
                            "video" -> {
                                // Video card — tap to play in system player
                                val context = androidx.compose.ui.platform.LocalContext.current
                                Box(
                                    modifier = Modifier
                                        .fillMaxWidth()
                                        .height(180.dp)
                                        .clip(RoundedCornerShape(RadiusMd))
                                        .background(Color(0xFF111111))
                                        .clickFeedback {
                                            try {
                                                val intent = android.content.Intent(android.content.Intent.ACTION_VIEW).apply {
                                                    setDataAndType(android.net.Uri.parse(media.url), "video/*")
                                                    addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
                                                }
                                                context.startActivity(intent)
                                            } catch (_: Exception) {
                                                // Fallback: open in browser
                                                val intent = android.content.Intent(android.content.Intent.ACTION_VIEW, android.net.Uri.parse(media.url))
                                                context.startActivity(intent)
                                            }
                                        }
                                        .padding(bottom = 6.dp),
                                    contentAlignment = Alignment.Center
                                ) {
                                    // Film strip decoration lines
                                    Column(
                                        modifier = Modifier.fillMaxWidth().padding(horizontal = 8.dp),
                                        verticalArrangement = Arrangement.Center,
                                        horizontalAlignment = Alignment.CenterHorizontally
                                    ) {
                                        // Play button
                                        Box(
                                            modifier = Modifier
                                                .size(56.dp)
                                                .clip(androidx.compose.foundation.shape.CircleShape)
                                                .background(BbxAccent.copy(alpha = 0.9f)),
                                            contentAlignment = Alignment.Center
                                        ) {
                                            Text("\u25B6", color = BbxWhite, fontSize = 24.sp)
                                        }
                                        Spacer(Modifier.height(8.dp))
                                        Text(
                                            "Tap to play video",
                                            style = MaterialTheme.typography.labelSmall,
                                            color = Neutral500
                                        )
                                    }
                                }
                            }
                            "audio" -> {
                                AudioPlayerBar(
                                    audioUrl = media.url,
                                    modifier = Modifier.padding(bottom = 6.dp)
                                )
                            }
                        }
                    }

                    // Render remaining text as markdown (with URLs stripped)
                    if (cleanContent.isNotBlank()) {
                        MarkdownText(
                            content = cleanContent,
                            modifier = Modifier
                                .fillMaxWidth()
                                .then(
                                    if (message.isStreaming && !message.isThinking) {
                                        Modifier.testTag(LIVE_ANSWER_EDGE_TAG)
                                    } else {
                                        Modifier
                                    },
                                )
                                .onGloballyPositioned { coordinates ->
                                    if (message.isStreaming && !message.isThinking) {
                                        onLiveEdgePositioned?.invoke(
                                            LiveTextSection.ANSWER,
                                            coordinates.boundsInWindow().bottom,
                                        )
                                    }
                                },
                        )
                        if (message.isStreaming) {
                            StreamingCursor(modifier = Modifier.padding(top = 2.dp))
                        }
                    } else if (message.isStreaming) {
                        Box(
                            modifier = Modifier
                                .testTag(LIVE_ANSWER_EDGE_TAG)
                                .onGloballyPositioned { coordinates ->
                                if (!message.isThinking) {
                                    onLiveEdgePositioned?.invoke(
                                        LiveTextSection.ANSWER,
                                        coordinates.boundsInWindow().bottom,
                                    )
                                }
                            },
                        ) {
                            StreamingCursor()
                        }
                    }
                }
            }
            if (!isUser && message.content.isBlank() && message.isStreaming && !message.isThinking) {
                Box(
                    modifier = Modifier
                        .testTag(LIVE_ANSWER_EDGE_TAG)
                        .onGloballyPositioned { coordinates ->
                        onLiveEdgePositioned?.invoke(
                            LiveTextSection.ANSWER,
                            coordinates.boundsInWindow().bottom,
                        )
                    },
                ) {
                    StreamingCursor()
                }
            }
            // NOTE: the pre-content "thinking" indicator is now "The Signal" line
            // rendered at the TOP of this column (above), gated on a live label —
            // so nothing more is needed here when content is still blank.

            // ── Artifact download chips (Phase 6b) ──
            // Native chips for files returned by /chat/save (artifacts[]); tapping
            // opens {baseUrl}{url} via ACTION_VIEW. Renders regardless of bubble text
            // so an artifact still surfaces even when the prose is empty.
            if (message.artifacts.isNotEmpty()) {
                ArtifactChips(artifacts = message.artifacts)
            }

            // ── TTS Audio Player (shown when ttsAudioUrl file exists) ──
            if (message.ttsAudioUrl != null && java.io.File(message.ttsAudioUrl).exists()) {
                AudioPlayerBar(
                    audioUrl = message.ttsAudioUrl,
                    modifier = Modifier.padding(top = 4.dp)
                )
            }

            // ── Context provenance (typed retrieval breakdown) ──
            // Shows "${N} context snapshots" with tap-to-expand sections for
            // Recent / Keyword / Semantic / Checkpoint SNAP-IDs.
            var provenanceExpanded by remember { mutableStateOf(false) }
            message.provenance?.let { prov ->
                if (!prov.isEmpty()) {
                    ContextProvenance(
                        provenance = prov,
                        expanded = provenanceExpanded,
                        onToggle = { provenanceExpanded = !provenanceExpanded },
                        onSnapshotClick = onSnapshotClick,
                    )
                }
            }

            // ── Timestamp + model ──
            // Portal: font-size 10px, color: var(--neutral-600)
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    text = formatTime(message.timestamp),
                    style = MaterialTheme.typography.labelSmall.copy(fontSize = 10.sp),
                    color = Neutral600
                )
                if (message.model != null) {
                    Text(
                        text = " \u00B7 ${message.model}",
                        style = MaterialTheme.typography.labelSmall.copy(fontSize = 10.sp),
                        color = Neutral600
                    )
                }
            }

            // \u2500\u2500 Long-press copy confirmation (USER bubbles) \u2500\u2500
            // User bubbles have no controls row, so the isCopied state surfaces as
            // a transient inline label instead of the copy-button checkmark.
            if (isUser && isCopied) {
                Text(
                    text = "\u2713 Copied",
                    style = MaterialTheme.typography.labelSmall.copy(fontSize = 10.sp),
                    color = HighlightKeyword
                )
            }

            } // end content Column

            // ── Action buttons ──
            // Portal: .bubble-controls with border-top separator, .bubble-btn 34px glass
            if (!isUser && message.content.isNotBlank() && !message.isStreaming) {
                // Separator line (matches Portal border-top: 1px solid rgba(255,255,255,0.06))
                Box(
                    Modifier
                        .fillMaxWidth()
                        .height(1.dp)
                        .background(GlassBorder)
                )

                Row(
                    horizontalArrangement = Arrangement.spacedBy(6.dp),
                    modifier = Modifier.padding(top = 4.dp)
                ) {
                    // Speak button: gray (idle) → red (generating) → green (ready/playing)
                    // Check if the TTS file actually exists (might be stale after reload)
                    val ttsFileExists = message.ttsAudioUrl?.let { java.io.File(it).exists() } ?: false
                    val hasTtsAudio = ttsFileExists
                    val isTtsGenerating = message.ttsGenerating
                    val speakColor by animateColorAsState(
                        targetValue = when {
                            hasTtsAudio -> SolidGreen
                            isTtsGenerating -> BbxAccent  // red accent = generating
                            else -> Neutral500
                        },
                        label = "speakColor"
                    )
                    val speakBg by animateColorAsState(
                        targetValue = when {
                            hasTtsAudio -> SolidGreen.copy(alpha = 0.15f)
                            isTtsGenerating -> BbxAccent.copy(alpha = 0.15f)
                            else -> Color(0x0DFFFFFF)
                        },
                        label = "speakBg"
                    )
                    val speakBorder by animateColorAsState(
                        targetValue = when {
                            hasTtsAudio -> SolidGreen.copy(alpha = 0.4f)
                            isTtsGenerating -> BbxAccent.copy(alpha = 0.4f)
                            else -> Color(0x14FFFFFF)
                        },
                        label = "speakBorder"
                    )

                    Box(
                        modifier = Modifier
                            .size(34.dp)
                            .background(speakBg, RoundedCornerShape(RadiusMd))
                            .border(1.dp, speakBorder, RoundedCornerShape(RadiusMd))
                            .clickFeedback {
                                android.util.Log.d("ChatBubble", "TTS BUTTON TAPPED: msg=${message.id.take(8)}, hasTts=$hasTtsAudio, generating=$isTtsGenerating")
                                if (hasTtsAudio) {
                                    com.aiblackbox.portal.data.voice.AudioPlaybackManager.loadAndPlay(message.ttsAudioUrl!!)
                                } else {
                                    onSpeakWithId(message.id, message.content)
                                }
                            },
                        contentAlignment = Alignment.Center
                    ) {
                        if (isTtsGenerating) {
                            androidx.compose.material3.CircularProgressIndicator(
                                modifier = Modifier.size(14.dp),
                                color = BbxAccent,
                                strokeWidth = 2.dp
                            )
                        } else {
                            SpeakerIcon(modifier = Modifier.size(18.dp), color = speakColor)
                        }
                    }

                    // Copy button (matches Portal .bubble-btn.copy-btn)
                    val copyColor by animateColorAsState(
                        targetValue = if (isCopied) HighlightKeyword else Neutral500,
                        label = "copyColor"
                    )
                    val copyBg by animateColorAsState(
                        targetValue = if (isCopied) HighlightKeyword.copy(alpha = 0.15f) else Color(0x0DFFFFFF),
                        label = "copyBg"
                    )
                    val copyBorder by animateColorAsState(
                        targetValue = if (isCopied) HighlightKeyword.copy(alpha = 0.4f) else Color(0x14FFFFFF),
                        label = "copyBorder"
                    )

                    Box(
                        modifier = Modifier
                            .size(34.dp)
                            .background(copyBg, RoundedCornerShape(RadiusMd))
                            .border(1.dp, copyBorder, RoundedCornerShape(RadiusMd))
                            .clickFeedback {
                                android.util.Log.d("ChatBubble", "COPY BUTTON TAPPED: msg=${message.id.take(8)}")
                                clipboardManager.setText(AnnotatedString(message.content))
                                isCopied = true
                            },
                        contentAlignment = Alignment.Center
                    ) {
                        if (isCopied) {
                            // Checkmark when copied
                            Text(
                                text = "\u2713",
                                color = HighlightKeyword,
                                fontWeight = FontWeight.Bold,
                                fontSize = 16.sp
                            )
                        } else {
                            CopyIcon(modifier = Modifier.size(18.dp), color = copyColor)
                        }
                    }
                }
            }
        }

        // ── Retry chip (failed user sends) ──
        // Rendered UNDER the bubble when the send failed with nothing usable
        // arrived. Tapping REPLACES the failed turn via onRetry (retryMessage).
        if (isUser && message.sendFailed) {
            Row(
                modifier = Modifier
                    .padding(top = 6.dp)
                    .clip(RoundedCornerShape(RadiusMd))
                    .background(BbxAccent.copy(alpha = 0.12f))
                    .border(1.dp, BbxAccent.copy(alpha = 0.4f), RoundedCornerShape(RadiusMd))
                    .clickFeedback(onClickLabel = "Retry sending message") { onRetry(message.id) }
                    .padding(horizontal = 12.dp, vertical = 6.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = "↻ Retry",
                    style = MaterialTheme.typography.labelMedium,
                    color = BbxAccent,
                    fontWeight = FontWeight.SemiBold
                )
            }
        }
        } // end outer layout Column
    }
}

// =============================================================================
// BubbleActionButton — matches Portal .bubble-btn
// 34x34dp, glass bg, subtle border, rounded-md
// =============================================================================
@Composable
private fun BubbleActionButton(
    onClick: () -> Unit,
    contentColor: Color = Neutral500,
    modifier: Modifier = Modifier,
    content: @Composable () -> Unit
) {
    Box(
        modifier = modifier
            .size(34.dp)
            .clip(RoundedCornerShape(RadiusMd))
            .background(Color(0x0DFFFFFF)) // rgba(255,255,255,0.05)
            .clickFeedback(onClick = onClick),
        contentAlignment = Alignment.Center
    ) {
        content()
    }
}

private fun formatTime(timestamp: Long): String {
    val sdf = java.text.SimpleDateFormat("h:mm a", java.util.Locale.US)
    return sdf.format(java.util.Date(timestamp))
}

// =============================================================================
// Artifact download chips (Phase 6b) — native Material3 AssistChips for the
// files returned by /chat/save (UiMessage.artifacts). Tapping a chip opens
// {baseUrl}{url} (e.g. http://host:9091/artifacts/<id>) via ACTION_VIEW,
// mirroring overlay/IntentActuator's open_url. baseUrl is the same value the
// rest of the bubble uses to resolve relative media (_cachedBaseUrl).
// =============================================================================
@Composable
private fun ArtifactChips(artifacts: List<ArtifactRef>) {
    val context = androidx.compose.ui.platform.LocalContext.current
    val feedback = rememberPressFeedback()
    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        artifacts.forEach { artifact ->
            val sizeLabel = formatSizeKb(artifact.sizeKb)
            val label = if (sizeLabel.isNotBlank()) {
                "\uD83D\uDCE5 ${artifact.filename} ($sizeLabel)"
            } else {
                "\uD83D\uDCE5 ${artifact.filename}"
            }
            AssistChip(
                onClick = {
                    feedback()
                    val fullUrl = if (artifact.url.startsWith("http")) artifact.url
                        else "${_cachedBaseUrl}${artifact.url}"
                    try {
                        val intent = android.content.Intent(android.content.Intent.ACTION_VIEW).apply {
                            data = android.net.Uri.parse(fullUrl)
                            addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
                        }
                        context.startActivity(intent)
                    } catch (_: Exception) {
                        // No activity can handle the view intent — fail silently.
                    }
                },
                label = {
                    Text(
                        text = label,
                        style = MaterialTheme.typography.labelMedium
                    )
                },
                colors = AssistChipDefaults.assistChipColors(
                    labelColor = BbxWhite,
                    containerColor = Color(0x14FFFFFF)
                ),
                border = null // no gray outline (HD/production look)
            )
        }
    }
}

/** Format an artifact size in KB for the chip label. Blank when 0/unknown. */
private fun formatSizeKb(sizeKb: Double): String = when {
    sizeKb <= 0.0 -> ""
    sizeKb >= 1024.0 -> String.format("%.1f MB", sizeKb / 1024.0)
    sizeKb >= 10.0 -> "${sizeKb.toInt()} KB"
    else -> String.format("%.1f KB", sizeKb)
}

// =============================================================================
// Inline media extraction — finds URLs of images/video/audio in message text,
// returns them as renderable media items, and strips them from the text.
// =============================================================================

private data class InlineMedia(val url: String, val type: String)

private val IMAGE_EXTENSIONS = setOf("png", "jpg", "jpeg", "webp", "gif", "bmp", "svg")
private val VIDEO_EXTENSIONS = setOf("mp4", "webm", "mov", "avi", "mkv")
private val AUDIO_EXTENSIONS = setOf("wav", "mp3", "m4a", "ogg", "flac", "aac")
private val ALL_MEDIA_EXTENSIONS = IMAGE_EXTENSIONS + VIDEO_EXTENSIONS + AUDIO_EXTENSIONS

// Matches: http(s) URLs, or relative /ui/uploads/ paths
private val URL_REGEX = Regex(
    """(https?://[^\s)\]>"]+|/ui/uploads/[^\s)\]>"]+)""",
    RegexOption.IGNORE_CASE
)

/**
 * baseUrl is needed to resolve relative /ui/uploads/ paths into absolute URLs
 * that AsyncImage/AudioPlayerBar can load.
 */
private var _cachedBaseUrl: String = ""
fun setChatBaseUrl(url: String) { _cachedBaseUrl = url }

private fun extractInlineMedia(
    content: String,
    alreadyRendered: Set<String> = emptySet(),
): Pair<String, List<InlineMedia>> {
    val media = mutableListOf<InlineMedia>()
    // Dedup: each unique media file renders once. Seeded with URLs already shown
    // (e.g. images in message.images), so the model echoing the generated URL in
    // prose + the task-resolution injecting it does NOT double-render.
    val seen = HashSet(alreadyRendered)
    var cleaned = content

    URL_REGEX.findAll(content).forEach { match ->
        val rawUrl = match.value.trimEnd('.', ',', ';', ':', '!', ')')
        val ext = rawUrl.substringAfterLast('.', "").substringBefore('?').lowercase()

        val type = when {
            ext in IMAGE_EXTENSIONS -> "image"
            ext in VIDEO_EXTENSIONS -> "video"
            ext in AUDIO_EXTENSIONS -> "audio"
            else -> null
        }

        if (type != null) {
            // Resolve relative URLs to absolute
            val resolvedUrl = if (rawUrl.startsWith("/")) {
                "${_cachedBaseUrl}$rawUrl"
            } else {
                rawUrl
            }

            if (seen.add(resolvedUrl.substringBefore('?'))) media.add(InlineMedia(resolvedUrl, type))
            // Remove the URL and any surrounding markdown image/link syntax
            cleaned = cleaned
                .replace(Regex("""!\[[^\]]*\]\(\s*\Q$rawUrl\E\s*\)"""), "")  // ![alt](url)
                .replace(Regex("""\[[^\]]*\]\(\s*\Q$rawUrl\E\s*\)"""), "")   // [text](url)
                .replace(rawUrl, "")  // bare URL
        }
    }

    // Clean up extra blank lines left behind
    cleaned = cleaned.replace(Regex("""\n{3,}"""), "\n\n").trim()

    return Pair(cleaned, media)
}
