package com.aiblackbox.portal.ui.components

import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxBlack
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.RadiusXl
import kotlinx.coroutines.delay

// =============================================================================
// ThinkingIndicator — aligned with Portal .bubble.thinking
//
// Portal thinking bubble:
//   background: linear-gradient(135deg, #000000 0%, #353535 100%)
//   border: 1px solid rgba(255,74,74,0.4)
//   animation: thinking-pulse 2s ease-in-out infinite
//   Cycles through THINKING_MESSAGES every 2.5s
//   3 animated dots + cycling hint text
// =============================================================================

// All 12 thinking messages from Portal chat-bubbles.js
private val THINKING_MESSAGES = listOf(
    "Processing thoughts...",
    "Searching memory...",
    "Analyzing context...",
    "Reasoning...",
    "Retrieving snapshots...",
    "Generating response...",
    "Thinking deeply...",
    "Connecting ideas...",
    "Examining patterns...",
    "Synthesizing information...",
    "Weighing options...",
    "Crafting response..."
)

private val ThinkingGradient = Brush.linearGradient(
    colors = listOf(BbxBlack, Color(0xFF353535))
)

private val ThinkingBorderColor = BbxAccent.copy(alpha = 0.4f) // rgba(255,74,74,0.4)

@Composable
fun ThinkingIndicator(
    isThinking: Boolean = true,
    modifier: Modifier = Modifier
) {
    if (!isThinking) return

    // Animation bubble only — animated dots + cycling placeholder hints.
    // The model's actual reasoning text lands in its own dedicated thinking
    // dropdown bubble (a separate UI element); duplicating it here was UX noise.
    var hintIndex by remember { mutableIntStateOf(0) }

    LaunchedEffect(isThinking) {
        while (isThinking) {
            delay(2500)
            hintIndex = (hintIndex + 1) % THINKING_MESSAGES.size
        }
    }

    // Breathing animation for the dots
    val infiniteTransition = rememberInfiniteTransition(label = "thinking")
    val breathe by infiniteTransition.animateFloat(
        initialValue = 0.4f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(
            animation = tween(1000),
            repeatMode = RepeatMode.Reverse
        ),
        label = "breathe"
    )

    // Pulse opacity for the whole bubble (matches Portal thinking-pulse animation)
    val pulseAlpha by infiniteTransition.animateFloat(
        initialValue = 0.85f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(
            animation = tween(2000),
            repeatMode = RepeatMode.Reverse
        ),
        label = "pulse"
    )

    // Thinking bubble with gradient background and red accent border
    Box(
        modifier = modifier
            .fillMaxWidth()
            .alpha(pulseAlpha)
            .clip(RoundedCornerShape(RadiusXl))
            .background(ThinkingGradient)
            .border(1.dp, ThinkingBorderColor, RoundedCornerShape(RadiusXl))
            .padding(horizontal = 16.dp, vertical = 14.dp)
    ) {
        Row(
            horizontalArrangement = Arrangement.spacedBy(4.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            // 3 animated dots with staggered breathing
            repeat(3) { index ->
                val dotAlpha = when (index) {
                    0 -> breathe
                    1 -> (breathe + 0.3f).coerceAtMost(1f)
                    else -> (breathe + 0.6f).coerceAtMost(1f)
                }
                Box(
                    modifier = Modifier
                        .size(6.dp)
                        .scale(0.8f + breathe * 0.2f)
                        .alpha(dotAlpha)
                        .background(BbxAccent, CircleShape)
                )
            }

            Spacer(Modifier.width(8.dp))

            AnimatedContent(
                targetState = hintIndex,
                transitionSpec = {
                    fadeIn(tween(500)) togetherWith fadeOut(tween(500))
                },
                label = "hint"
            ) { index ->
                Text(
                    text = THINKING_MESSAGES[index],
                    style = MaterialTheme.typography.bodySmall,
                    color = Neutral500
                )
            }
        }
    }
}
