package com.aiblackbox.portal.ui.components

import android.view.HapticFeedbackConstants
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.animateContentSize
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.spring
import androidx.compose.animation.core.tween
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import com.aiblackbox.portal.ui.feedback.clickFeedback
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
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.aiblackbox.portal.data.model.TaskStatus
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.Neutral100
import com.aiblackbox.portal.ui.theme.Neutral200
import com.aiblackbox.portal.ui.theme.Neutral300
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.RadiusLg
import com.aiblackbox.portal.ui.theme.RadiusMd
import com.aiblackbox.portal.ui.theme.SolidGreen
import com.aiblackbox.portal.ui.theme.glassSurface

// =============================================================================
// TaskPanel — Collapsible bubble matching Portal task-manager.js
//
// Collapsed: small pill showing task count + pulsing dot
// Expanded: full task list with progress, icons, status
// Tap bubble to toggle. Minimal footprint when collapsed.
// =============================================================================

@Composable
fun TaskPanel(
    tasks: List<TaskStatus>,
    visible: Boolean,
    onDismiss: () -> Unit,
    modifier: Modifier = Modifier
) {
    val view = LocalView.current
    var isExpanded by remember { mutableStateOf(false) }

    // Auto-collapse when no tasks
    if (tasks.isEmpty()) isExpanded = false

    AnimatedVisibility(
        visible = visible && tasks.isNotEmpty(),
        enter = slideInVertically { it },
        exit = slideOutVertically { it }
    ) {
        val hasActive = tasks.any { it.status.equals("processing", true) || it.status.equals("pending", true) }

        Box(
            modifier = modifier.fillMaxWidth(),
            contentAlignment = Alignment.CenterEnd
        ) {
            Column(
                modifier = Modifier
                    .widthIn(max = if (isExpanded) 300.dp else 140.dp)
                    .animateContentSize(animationSpec = spring(dampingRatio = 0.8f))
                    .then(
                        if (!isExpanded && hasActive) {
                            // Accent glow border on collapsed active bubble
                            Modifier.border(
                                1.5.dp,
                                BbxAccent.copy(alpha = 0.5f),
                                RoundedCornerShape(24.dp)
                            )
                        } else if (isExpanded) {
                            Modifier.border(
                                1.dp,
                                GlassBorder,
                                RoundedCornerShape(RadiusLg)
                            )
                        } else Modifier
                    )
                    .clip(RoundedCornerShape(if (isExpanded) RadiusLg else 24.dp))
                    .background(
                        if (!isExpanded) {
                            androidx.compose.ui.graphics.Brush.horizontalGradient(
                                colors = listOf(
                                    Neutral200,
                                    Neutral100.copy(alpha = 0.95f)
                                )
                            )
                        } else {
                            androidx.compose.ui.graphics.Brush.verticalGradient(
                                colors = listOf(
                                    Neutral200.copy(alpha = 0.95f),
                                    Neutral100
                                )
                            )
                        }
                    )
                    .clickFeedback {
                        isExpanded = !isExpanded
                    }
                    .padding(if (isExpanded) 12.dp else 10.dp),
                horizontalAlignment = if (isExpanded) Alignment.CenterHorizontally else Alignment.Start
            ) {
                if (!isExpanded) {
                    // ── Collapsed: compact pill with count + type summary ──
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.Center,
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        // Pulsing accent dot for active tasks
                        if (hasActive) {
                            val pulse = rememberInfiniteTransition(label = "bubblePulse")
                            val pulseAlpha by pulse.animateFloat(
                                initialValue = 0.3f, targetValue = 1f,
                                animationSpec = infiniteRepeatable(tween(700), RepeatMode.Reverse),
                                label = "alpha"
                            )
                            Box(
                                Modifier
                                    .size(10.dp)
                                    .alpha(pulseAlpha)
                                    .clip(CircleShape)
                                    .background(BbxAccent)
                            )
                            Spacer(Modifier.width(8.dp))
                        }
                        Text(
                            text = "${tasks.size}",
                            style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.Bold),
                            color = BbxWhite
                        )
                        Spacer(Modifier.width(6.dp))
                        Text(
                            text = if (tasks.size == 1) "task" else "tasks",
                            style = MaterialTheme.typography.labelMedium,
                            color = Neutral500
                        )
                    }
                } else {
                    // ── Expanded: full task list ──
                    // Header
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            if (hasActive) {
                                val pulse = rememberInfiniteTransition(label = "headerPulse")
                                val pulseAlpha by pulse.animateFloat(
                                    initialValue = 0.4f, targetValue = 1f,
                                    animationSpec = infiniteRepeatable(tween(1000), RepeatMode.Reverse),
                                    label = "hPulse"
                                )
                                Box(
                                    Modifier
                                        .size(8.dp)
                                        .alpha(pulseAlpha)
                                        .clip(CircleShape)
                                        .background(BbxAccent)
                                )
                                Spacer(Modifier.width(8.dp))
                            }
                            Text(
                                "Tasks (${tasks.size})",
                                style = MaterialTheme.typography.labelLarge.copy(fontWeight = FontWeight.SemiBold),
                                color = BbxWhite
                            )
                        }
                        // Close X — collapses back to bubble
                        Text(
                            "\u2715",
                            color = Neutral500,
                            fontSize = 14.sp,
                            modifier = Modifier
                                .clip(CircleShape)
                                .clickFeedback {
                                    isExpanded = false
                                }
                                .padding(4.dp)
                        )
                    }

                    Spacer(Modifier.height(8.dp))

                    // Task items
                    tasks.forEach { task ->
                        TaskItem(task)
                        Spacer(Modifier.height(6.dp))
                    }
                }
            }
        }
    }
}

@Composable
private fun TaskItem(task: TaskStatus) {
    val isActive = task.status.equals("processing", true) || task.status.equals("pending", true)
    val isComplete = task.status.equals("completed", true)
    val isFailed = task.status.equals("failed", true)
    // 'cancelled' is terminal and DISTINCT from failed (G2-T8): an
    // operator-cancelled task must not render as a crash (red ✗) or as blank.
    val isCancelled = task.status.equals("cancelled", true)

    val borderColor by animateColorAsState(
        targetValue = when {
            isComplete -> SolidGreen.copy(alpha = 0.3f)
            isFailed -> BbxAccent.copy(alpha = 0.3f)
            isCancelled -> GlassBorder.copy(alpha = 0.5f)   // muted, not alarming
            isActive -> BbxAccent.copy(alpha = 0.15f)
            else -> GlassBorder
        },
        label = "borderColor"
    )

    val (typeIcon, typeLabel) = getTaskTypeInfo(task.taskType)

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(RadiusMd))
            .background(Neutral200.copy(alpha = 0.5f))
            .border(1.dp, borderColor, RoundedCornerShape(RadiusMd))
            .padding(8.dp)
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier.fillMaxWidth()
        ) {
            Text(text = typeIcon, fontSize = 14.sp)
            Spacer(Modifier.width(6.dp))
            Text(
                text = typeLabel,
                style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Medium),
                color = BbxWhite,
                modifier = Modifier.weight(1f)
            )

            // Status indicator
            when {
                isComplete -> Text("\u2713", color = SolidGreen, fontWeight = FontWeight.Bold, fontSize = 14.sp)
                isFailed -> Text("\u2717", color = BbxAccent, fontWeight = FontWeight.Bold, fontSize = 14.sp)
                isCancelled -> Text("\u2298", color = BbxWhite.copy(alpha = 0.5f), fontWeight = FontWeight.Bold, fontSize = 14.sp)  // \u2298 cancelled
                isActive -> {
                    val spin = rememberInfiniteTransition(label = "spin")
                    val spinAlpha by spin.animateFloat(
                        initialValue = 0.3f, targetValue = 1f,
                        animationSpec = infiniteRepeatable(tween(800), RepeatMode.Reverse),
                        label = "sAlpha"
                    )
                    Box(
                        Modifier
                            .size(8.dp)
                            .alpha(spinAlpha)
                            .clip(CircleShape)
                            .background(BbxAccent)
                    )
                }
            }
        }

        // Progress bar
        if (isActive && task.progress > 0) {
            Spacer(Modifier.height(4.dp))
            LinearProgressIndicator(
                progress = { task.progress / 100f },
                modifier = Modifier
                    .fillMaxWidth()
                    .height(2.dp)
                    .clip(RoundedCornerShape(1.dp)),
                color = BbxAccent,
                trackColor = Neutral300
            )
            Text(
                "${task.progress}%",
                style = MaterialTheme.typography.labelSmall,
                color = Neutral500,
                fontSize = 10.sp,
                modifier = Modifier.padding(top = 1.dp)
            )
        }

        // Error
        if (isFailed && task.error != null) {
            Spacer(Modifier.height(3.dp))
            Text(
                task.error!!,
                style = MaterialTheme.typography.labelSmall,
                color = BbxAccent,
                maxLines = 1,
                fontSize = 10.sp
            )
        }
    }
}

private fun getTaskTypeInfo(taskType: String?): Pair<String, String> = when (taskType?.lowercase()) {
    "image", "image_generation" -> "\uD83C\uDFA8" to "Image"
    "video", "video_generation" -> "\uD83C\uDFA5" to "Video"
    "music", "music_generation" -> "\uD83C\uDFB5" to "Music"
    "audio", "audio_analysis" -> "\uD83D\uDD0A" to "Audio"
    "video_analysis" -> "\uD83D\uDCF9" to "Video Analysis"
    "gemini_tts" -> "\uD83D\uDDE3" to "TTS"
    "browser_use", "computer_use" -> "\uD83D\uDCBB" to "Computer Use"
    else -> "\u2699\uFE0F" to (taskType?.replace("_", " ")?.replaceFirstChar { it.uppercase() } ?: "Task")
}
