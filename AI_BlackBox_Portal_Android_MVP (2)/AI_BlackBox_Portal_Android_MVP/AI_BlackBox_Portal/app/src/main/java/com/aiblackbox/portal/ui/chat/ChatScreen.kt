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
    val listState = rememberLazyListState()
    val scope = rememberCoroutineScope()
    val view = LocalView.current

    var peekSnapId by remember { mutableStateOf<String?>(null) }

    // Initialize API client + set base URL for inline media resolution
    LaunchedEffect(origin) {
        viewModel.initialize(origin)
        com.aiblackbox.portal.ui.components.setChatBaseUrl(origin)
    }

    // Detect if user is scrolled away from the bottom
    val isAtBottom by remember {
        derivedStateOf {
            val lastVisible = listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0
            val totalItems = listState.layoutInfo.totalItemsCount
            totalItems == 0 || lastVisible >= totalItems - 2
        }
    }

    // Auto-scroll: snap instantly when new messages arrive (no jiggle animation)
    LaunchedEffect(messages.size) {
        if (messages.isNotEmpty()) {
            listState.scrollToItem(messages.size - 1)
        }
    }

    // During streaming, smoothly follow the growing content of the last message
    val isStreaming = chatState == ChatState.STREAMING || chatState == ChatState.THINKING
    val lastContentLength = messages.lastOrNull()?.content?.length ?: 0
    LaunchedEffect(lastContentLength) {
        if (isStreaming && isAtBottom && messages.isNotEmpty()) {
            listState.scrollToItem(messages.size - 1)
        }
    }

    if (messages.isEmpty()) {
        HomeScreen(modifier = modifier)
    } else {
        Box(modifier = modifier.fillMaxSize()) {
            // Ember glow behind the messages while the AI is thinking/streaming.
            // The chat screen is transparent over the activity's BbxBlack root and
            // bubbles carry their own backgrounds, so embers show through the gaps.
            EmberOverlay(active = isStreaming, modifier = Modifier.matchParentSize())

            LazyColumn(
                state = listState,
                modifier = Modifier.fillMaxSize(),
                contentPadding = PaddingValues(top = 8.dp, bottom = 200.dp)
            ) {
                items(
                    items = messages,
                    key = { it.id }
                ) { message ->
                    ChatBubble(
                        message = message,
                        onSpeak = onSpeak,
                        onSpeakWithId = onSpeakWithId,
                        onSnapshotClick = { peekSnapId = it }
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
                                listState.animateScrollToItem(messages.size - 1)
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
