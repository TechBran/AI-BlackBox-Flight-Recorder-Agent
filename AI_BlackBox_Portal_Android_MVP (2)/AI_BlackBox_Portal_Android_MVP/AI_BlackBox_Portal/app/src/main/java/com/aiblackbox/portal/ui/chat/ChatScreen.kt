package com.aiblackbox.portal.ui.chat

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.dp
import com.aiblackbox.portal.data.model.UiMessage
import com.aiblackbox.portal.ui.components.ChatBubble
import com.aiblackbox.portal.ui.components.EmberOverlay
import com.aiblackbox.portal.ui.components.LiveTextSection
import com.aiblackbox.portal.ui.components.SnapshotPeekSheet

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
    bottomFocalGeometry: BottomFocalGeometry? = null,
    modifier: Modifier = Modifier
) {
    val messages by viewModel.messages.collectAsState()
    val chatState by viewModel.chatState.collectAsState()
    // "The Signal" — transient, presentation-only telemetry label for the live
    // turn. Passed ONLY to the streaming bubble below; never persisted on a message.
    val signalLabel by viewModel.signalLabel.collectAsState()
    val listState = rememberLazyListState()

    var peekSnapId by remember { mutableStateOf<String?>(null) }

    // Initialize API client + set base URL for inline media resolution
    LaunchedEffect(origin) {
        viewModel.initialize(origin)
        com.aiblackbox.portal.ui.components.setChatBaseUrl(origin)
    }

    val isStreaming = chatState == ChatState.STREAMING || chatState == ChatState.THINKING

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
            MainChatContent(
                messages = messages,
                chatState = chatState,
                signalLabel = signalLabel,
                listState = listState,
                onSpeak = onSpeak,
                onSpeakWithId = onSpeakWithId,
                onSnapshotClick = { peekSnapId = it },
                onRetry = viewModel::retryMessage,
                bottomFocalGeometry = bottomFocalGeometry,
            )
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

@Composable
internal fun MainChatContent(
    messages: List<UiMessage>,
    chatState: ChatState,
    signalLabel: String?,
    modifier: Modifier = Modifier,
    listState: LazyListState = rememberLazyListState(),
    onSpeak: (String) -> Unit = {},
    onSpeakWithId: (String, String) -> Unit = { _, _ -> },
    onSnapshotClick: (String) -> Unit = {},
    onRetry: (String) -> Unit = {},
    bottomFocalGeometry: BottomFocalGeometry? = null,
) {
    val liveMessage = messages.lastOrNull { it.role == "assistant" }
    val liveSnapshot = LiveStreamSnapshot(
        messageId = liveMessage?.id,
        reasoningLength = liveMessage?.reasoning?.length ?: 0,
        answerLength = liveMessage?.content?.length ?: 0,
        phase = when (chatState) {
            ChatState.THINKING -> LiveStreamPhase.THINKING
            ChatState.STREAMING -> LiveStreamPhase.ANSWERING
            else -> LiveStreamPhase.IDLE
        },
        statusLabel = signalLabel,
    )
    val followState = rememberLiveStreamFollowState(listState, liveSnapshot)
    val density = LocalDensity.current
    val bottomClearance = bottomFocalGeometry?.let {
        with(density) { (it.residenceBottomPx - it.composerTopPx).coerceAtLeast(0f).toDp() }
    } ?: (FALLBACK_COMPOSER_HEIGHT + SIGNAL_RESIDENCE_HEIGHT)

    Box(modifier.fillMaxSize()) {
        LazyColumn(
            state = listState,
            modifier = Modifier
                .fillMaxSize()
                .liveStreamUserInput(followState)
                .testTag("messages"),
            contentPadding = PaddingValues(top = 8.dp, bottom = bottomClearance),
        ) {
            items(items = messages, key = { it.id }) { message ->
                val isLiveTurn = message.id == liveSnapshot.messageId
                val expectedSection = when (liveSnapshot.phase) {
                    LiveStreamPhase.THINKING -> LiveTextSection.REASONING
                    LiveStreamPhase.ANSWERING -> LiveTextSection.ANSWER
                    else -> null
                }
                ChatBubble(
                    message = message,
                    onSpeak = onSpeak,
                    onSpeakWithId = onSpeakWithId,
                    onSnapshotClick = onSnapshotClick,
                    onRetry = onRetry,
                    signalLabel = if (isLiveTurn) signalLabel else null,
                    onLiveEdgePositioned = if (isLiveTurn && expectedSection != null) {
                        { section, y -> if (section == expectedSection) followState.reportEdge(y) }
                    } else null,
                )
            }
        }
        LiveStreamFocalRail(
            signalLabel,
            followState,
            liveTargetYPx = bottomFocalGeometry?.liveTargetYPx,
        )
    }
}
