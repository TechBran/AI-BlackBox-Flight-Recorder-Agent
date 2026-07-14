package com.aiblackbox.portal.ui.chat

import android.view.HapticFeedbackConstants
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.animation.scaleOut
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import com.aiblackbox.portal.ui.feedback.clickFeedback
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.aiblackbox.portal.ui.components.ChatBubble
import com.aiblackbox.portal.ui.components.EmberOverlay
import com.aiblackbox.portal.ui.components.SnapshotPeekSheet
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxWhite
import kotlinx.coroutines.launch

/**
 * ChatScreen displays the message list.
 * IMPORTANT: The [viewModel] parameter MUST be passed from the parent (NativeMainActivity)
 * to share the same instance with the Composer bottomBar. Do NOT use a default viewModel()
 * here — that would create a separate navigation-scoped instance, causing the Composer
 * to update one ViewModel while this screen observes a different one.
 */
@Composable
fun ChatScreen(
    origin: String,
    operator: String,
    viewModel: ChatViewModel,
    onSpeak: (String) -> Unit = {},
    onSpeakWithId: (String, String) -> Unit = { _, _ -> },
    modifier: Modifier = Modifier
) {
    val messages by viewModel.messages.collectAsState()
    val chatState by viewModel.chatState.collectAsState()
    // "The Signal" — transient, presentation-only telemetry label for the live
    // turn. Passed ONLY to the streaming bubble below; never persisted on a message.
    val signalLabel by viewModel.signalLabel.collectAsState()
    val listState = rememberLazyListState()
    val scope = rememberCoroutineScope()
    val view = LocalView.current

    var peekSnapId by remember { mutableStateOf<String?>(null) }

    // Initialize API client + set base URL for inline media resolution
    LaunchedEffect(origin) {
        viewModel.initialize(origin)
        com.aiblackbox.portal.ui.components.setChatBaseUrl(origin)
    }

    // Detect whether the END of the conversation is visible. EDGE-based (is the last
    // item's BOTTOM within the viewport), not index-based: a single streaming message
    // can grow taller than the screen, so an index check would always read "at bottom"
    // and the auto-follow would keep yanking the user back when they try to scroll up.
    val isAtBottom by remember {
        derivedStateOf {
            val info = listState.layoutInfo
            val last = info.visibleItemsInfo.lastOrNull()
            last == null ||
                (last.index >= info.totalItemsCount - 1 &&
                    last.offset + last.size <= info.viewportEndOffset)
        }
    }

    // Auto-scroll: snap instantly when new messages arrive (no jiggle animation).
    // Int.MAX_VALUE offset sticks to the BOTTOM of the last item (Compose clamps to the
    // true end, respecting the bottom content padding), so a last message taller than
    // the screen still shows its newest end rather than top-pinning to its start.
    LaunchedEffect(messages.size) {
        if (messages.isNotEmpty()) {
            listState.scrollToItem(messages.size - 1, Int.MAX_VALUE)
        }
    }

    // During streaming, smoothly follow the growing content of the last message
    val isStreaming = chatState == ChatState.STREAMING || chatState == ChatState.THINKING
    val lastContentLength = messages.lastOrNull()?.content?.length ?: 0
    LaunchedEffect(lastContentLength) {
        if (isStreaming && isAtBottom && messages.isNotEmpty()) {
            // Stick to the BOTTOM of the growing last message so the newest text keeps
            // following the screen even after the message exceeds the viewport height
            // (plain scrollToItem(index) top-pins it → the view appeared to "freeze").
            listState.scrollToItem(messages.size - 1, Int.MAX_VALUE)
        }
    }

    // One ember backdrop behind BOTH the empty/home state and the messages, so
    // "Always on" shows embers everywhere — including the BlackBox-logo home screen.
    // LocalEmberMode (read inside EmberOverlay) decides: Always = on; While
    // generating = only while streaming; Off = never. The home screen + chat content
    // are transparent over the activity's BbxBlack root, so embers show through;
    // bubbles carry their own backgrounds and stay readable.
    Box(modifier = modifier.fillMaxSize()) {
        EmberOverlay(active = isStreaming, modifier = Modifier.matchParentSize())

        if (messages.isEmpty()) {
            HomeScreen(modifier = Modifier.fillMaxSize())
        } else {
            LazyColumn(
                state = listState,
                modifier = Modifier.fillMaxSize(),
                contentPadding = PaddingValues(top = 8.dp, bottom = 200.dp)
            ) {
                // The newest assistant message is the live turn that owns The Signal
                // (during streaming AND for the brief post-answer mint flourish).
                val liveSignalTurnId = messages.lastOrNull()?.takeIf { it.role == "assistant" }?.id
                items(
                    items = messages,
                    key = { it.id }
                ) { message ->
                    // Only the NEWEST assistant turn shows The Signal (its live
                    // telemetry during the turn + the brief post-answer mint line);
                    // every other bubble gets null so a label change never recomposes
                    // them. Keyed on the newest message so the mint line — pushed
                    // AFTER the turn ends — still lands on the right bubble.
                    val isLiveTurn = message.id == liveSignalTurnId
                    ChatBubble(
                        message = message,
                        onSpeak = onSpeak,
                        onSpeakWithId = onSpeakWithId,
                        onSnapshotClick = { peekSnapId = it },
                        // Retry a failed send: REPLACES the failed turn (removes the
                        // error bubble + failed user msg, re-fires same text+images).
                        onRetry = viewModel::retryMessage,
                        signalLabel = if (isLiveTurn) signalLabel else null,
                    )
                }
            }

            // ── Scroll-to-bottom FAB — appears when scrolled up ──
            AnimatedVisibility(
                visible = !isAtBottom && messages.size > 2,
                modifier = Modifier
                    .align(Alignment.BottomCenter)
                    .padding(bottom = 210.dp),
                enter = fadeIn() + scaleIn(),
                exit = fadeOut() + scaleOut()
            ) {
                Box(
                    modifier = Modifier
                        .size(40.dp)
                        .clip(CircleShape)
                        .background(BbxAccent)
                        .clickFeedback {
                            scope.launch {
                                // MAX offset → animate to the true bottom (handles a
                                // last message taller than the screen, like the auto-follow)
                                listState.animateScrollToItem(messages.size - 1, Int.MAX_VALUE)
                            }
                        },
                    contentAlignment = Alignment.Center
                ) {
                    Text(
                        "\u2193",
                        color = BbxWhite,
                        fontSize = 20.sp,
                        fontWeight = FontWeight.Bold
                    )
                }
            }
        }
    }

    peekSnapId?.let { snapId ->
        SnapshotPeekSheet(
            snapId = snapId,
            origin = origin,
            onDismiss = { peekSnapId = null }
        )
    }
}
