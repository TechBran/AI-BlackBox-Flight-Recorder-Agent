package com.aiblackbox.portal.ui.generation

import android.view.HapticFeedbackConstants
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import com.aiblackbox.portal.ui.feedback.clickFeedback
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aiblackbox.portal.ui.components.EmberBackdrop
import com.aiblackbox.portal.ui.components.GlassCard
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.DurationBase
import com.aiblackbox.portal.ui.theme.EaseStandard
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.GlassFloatingBubble
import com.aiblackbox.portal.ui.theme.GlowBlue
import com.aiblackbox.portal.ui.theme.Neutral100
import com.aiblackbox.portal.ui.theme.Neutral200
import com.aiblackbox.portal.ui.theme.Neutral300
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.PillShape
import com.aiblackbox.portal.ui.theme.RadiusLg
import com.aiblackbox.portal.ui.theme.RadiusMd
import com.aiblackbox.portal.ui.theme.SolidGreen
import com.aiblackbox.portal.ui.theme.glassSurface

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun VideoGenScreen(
    origin: String,
    modifier: Modifier = Modifier,
    viewModel: GenerationViewModel = viewModel()
) {
    val view = LocalView.current
    var prompt by remember { mutableStateOf("") }
    var selectedRatio by remember { mutableStateOf("16:9") }
    var selectedDuration by remember { mutableIntStateOf(6) }
    val state by viewModel.state.collectAsState()
    val resultUrl by viewModel.resultUrl.collectAsState()
    val error by viewModel.error.collectAsState()
    val taskStatus by viewModel.taskStatus.collectAsState()

    // Color for the info banner (blue tint matching Portal's video-gen-info)
    val infoBannerBg = GlowBlue.copy(alpha = 0.15f)
    val infoBorderColor = GlowBlue

    LaunchedEffect(origin) { viewModel.initialize(origin) }

    // Ember backdrop behind the (scrolling) content while generating. The
    // overlay is a SIBLING of the scroll, so it stays fixed full-screen.
    EmberBackdrop(
        active = state == GenState.SUBMITTING || state == GenState.POLLING,
        modifier = modifier,
    ) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(start = 16.dp, end = 16.dp, bottom = 16.dp, top = 100.dp)
    ) {
        // Header
        Text(
            "Video Generation",
            style = MaterialTheme.typography.headlineMedium.copy(fontWeight = FontWeight.Bold),
            color = BbxWhite
        )
        Spacer(Modifier.height(4.dp))
        Text(
            "Veo 3.1",
            style = MaterialTheme.typography.bodySmall,
            color = Neutral500
        )
        Spacer(Modifier.height(16.dp))

        // Info banner (matches Portal's video-gen-info blue banner)
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .clip(RoundedCornerShape(RadiusMd))
                .background(infoBannerBg)
                .border(
                    width = 1.dp,
                    color = infoBorderColor.copy(alpha = 0.3f),
                    shape = RoundedCornerShape(RadiusMd)
                )
                .padding(12.dp)
        ) {
            Text(
                "Video generation takes 5-20 minutes. You can close this screen and check back later.",
                style = MaterialTheme.typography.bodySmall,
                color = BbxDim,
                lineHeight = 18.sp
            )
        }
        Spacer(Modifier.height(16.dp))

        // Prompt input
        GlassCard(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(RadiusMd)
        ) {
            Column(modifier = Modifier.padding(14.dp)) {
                Text(
                    "Prompt",
                    style = MaterialTheme.typography.labelMedium,
                    color = BbxDim
                )
                Spacer(Modifier.height(8.dp))
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(min = 100.dp)
                        .clip(RoundedCornerShape(RadiusMd))
                        .background(Neutral100)
                        .border(1.dp, GlassBorder, RoundedCornerShape(RadiusMd))
                        .padding(12.dp)
                ) {
                    if (prompt.isEmpty()) {
                        Text(
                            "Describe the video scene...",
                            color = Neutral500,
                            style = MaterialTheme.typography.bodyLarge
                        )
                    }
                    BasicTextField(
                        value = prompt,
                        onValueChange = { prompt = it },
                        modifier = Modifier.fillMaxWidth(),
                        textStyle = MaterialTheme.typography.bodyLarge.copy(color = BbxWhite),
                        cursorBrush = SolidColor(BbxAccent)
                    )
                }
            }
        }
        Spacer(Modifier.height(16.dp))

        // Settings card
        GlassCard(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(RadiusMd)
        ) {
            Column(modifier = Modifier.padding(14.dp)) {
                // Duration selector
                Text(
                    "Duration",
                    style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Medium),
                    color = BbxDim
                )
                Spacer(Modifier.height(10.dp))
                FlowRow(
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    listOf(4, 6, 8).forEach { dur ->
                        val selected = dur == selectedDuration
                        val interactionSource = remember { MutableInteractionSource() }
                        val pressed by interactionSource.collectIsPressedAsState()
                        val chipScale by animateFloatAsState(
                            targetValue = if (pressed) 0.92f else 1f,
                            animationSpec = tween(DurationBase, easing = EaseStandard),
                            label = "durScale"
                        )
                        val chipBg by animateColorAsState(
                            targetValue = if (selected) BbxAccent.copy(alpha = 0.18f) else Neutral200,
                            animationSpec = tween(DurationBase, easing = EaseStandard),
                            label = "durBg"
                        )
                        val chipBorder by animateColorAsState(
                            targetValue = if (selected) BbxAccent.copy(alpha = 0.5f) else Neutral300,
                            animationSpec = tween(DurationBase, easing = EaseStandard),
                            label = "durBorder"
                        )

                        Box(
                            modifier = Modifier
                                .scale(chipScale)
                                .clip(PillShape)
                                .background(chipBg)
                                .border(1.dp, chipBorder, PillShape)
                                .clickFeedback(
                                    interactionSource = interactionSource,
                                    indication = null
                                ) {
                                    selectedDuration = dur
                                }
                                .padding(horizontal = 16.dp, vertical = 8.dp)
                        ) {
                            Text(
                                "${dur}s",
                                style = MaterialTheme.typography.labelMedium.copy(
                                    fontWeight = if (selected) FontWeight.Bold else FontWeight.Normal
                                ),
                                color = if (selected) BbxAccent else Neutral500
                            )
                        }
                    }
                }
                Spacer(Modifier.height(18.dp))

                // Aspect Ratio selector
                Text(
                    "Aspect Ratio",
                    style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Medium),
                    color = BbxDim
                )
                Spacer(Modifier.height(10.dp))
                FlowRow(
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    listOf("16:9", "9:16", "1:1").forEach { ratio ->
                        val selected = ratio == selectedRatio
                        val interactionSource = remember { MutableInteractionSource() }
                        val pressed by interactionSource.collectIsPressedAsState()
                        val chipScale by animateFloatAsState(
                            targetValue = if (pressed) 0.92f else 1f,
                            animationSpec = tween(DurationBase, easing = EaseStandard),
                            label = "ratioScale"
                        )
                        val chipBg by animateColorAsState(
                            targetValue = if (selected) BbxAccent.copy(alpha = 0.18f) else Neutral200,
                            animationSpec = tween(DurationBase, easing = EaseStandard),
                            label = "ratioBg"
                        )
                        val chipBorder by animateColorAsState(
                            targetValue = if (selected) BbxAccent.copy(alpha = 0.5f) else Neutral300,
                            animationSpec = tween(DurationBase, easing = EaseStandard),
                            label = "ratioBorder"
                        )

                        Box(
                            modifier = Modifier
                                .scale(chipScale)
                                .clip(PillShape)
                                .background(chipBg)
                                .border(1.dp, chipBorder, PillShape)
                                .clickFeedback(
                                    interactionSource = interactionSource,
                                    indication = null
                                ) {
                                    selectedRatio = ratio
                                }
                                .padding(horizontal = 16.dp, vertical = 8.dp)
                        ) {
                            Text(
                                ratio,
                                style = MaterialTheme.typography.labelMedium.copy(
                                    fontWeight = if (selected) FontWeight.Bold else FontWeight.Normal
                                ),
                                color = if (selected) BbxAccent else Neutral500
                            )
                        }
                    }
                }
            }
        }
        Spacer(Modifier.height(20.dp))

        // Generate button
        val btnInteraction = remember { MutableInteractionSource() }
        val btnPressed by btnInteraction.collectIsPressedAsState()
        val btnScale by animateFloatAsState(
            targetValue = if (btnPressed) 0.96f else 1f,
            animationSpec = tween(DurationBase, easing = EaseStandard),
            label = "btnScale"
        )
        val isGenerating = state == GenState.SUBMITTING || state == GenState.POLLING
        val btnEnabled = prompt.isNotBlank() && !isGenerating

        Box(
            modifier = Modifier
                .fillMaxWidth()
                .scale(btnScale)
                .clip(RoundedCornerShape(RadiusLg))
                .background(
                    if (btnEnabled) BbxAccent
                    else BbxAccent.copy(alpha = 0.4f)
                )
                .clickFeedback(
                    interactionSource = btnInteraction,
                    indication = null,
                    enabled = btnEnabled
                ) {
                    viewModel.generateVideo(prompt, selectedRatio, selectedDuration)
                }
                .padding(vertical = 14.dp),
            contentAlignment = Alignment.Center
        ) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.Center
            ) {
                if (isGenerating) {
                    CircularProgressIndicator(
                        modifier = Modifier.size(18.dp),
                        color = BbxWhite,
                        strokeWidth = 2.dp
                    )
                    Spacer(Modifier.width(10.dp))
                }
                Text(
                    when (state) {
                        GenState.IDLE -> "Generate Video"
                        GenState.SUBMITTING -> "Submitting..."
                        GenState.POLLING -> "Generating... ${taskStatus?.progress ?: 0}%"
                        GenState.COMPLETED -> "Done!"
                        GenState.FAILED -> "Retry"
                    },
                    style = MaterialTheme.typography.labelLarge.copy(
                        fontWeight = FontWeight.Bold,
                        fontSize = 15.sp
                    ),
                    color = BbxWhite
                )
            }
        }

        // Error display
        AnimatedVisibility(
            visible = error != null,
            enter = fadeIn() + scaleIn(initialScale = 0.95f),
            exit = fadeOut()
        ) {
            error?.let {
                Spacer(Modifier.height(10.dp))
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .clip(RoundedCornerShape(RadiusMd))
                        .background(BbxAccent.copy(alpha = 0.1f))
                        .border(1.dp, BbxAccent.copy(alpha = 0.3f), RoundedCornerShape(RadiusMd))
                        .padding(12.dp)
                ) {
                    Text(it, color = BbxAccent, style = MaterialTheme.typography.bodySmall)
                }
            }
        }

        // Status info when polling
        AnimatedVisibility(
            visible = state == GenState.POLLING,
            enter = fadeIn(),
            exit = fadeOut()
        ) {
            Column(modifier = Modifier.padding(top = 12.dp)) {
                taskStatus?.let { status ->
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(RadiusMd))
                            .background(BbxAccent.copy(alpha = 0.05f))
                            .padding(12.dp)
                    ) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            CircularProgressIndicator(
                                modifier = Modifier.size(14.dp),
                                color = BbxAccent,
                                strokeWidth = 2.dp
                            )
                            Spacer(Modifier.width(10.dp))
                            Column {
                                Text(
                                    "Task ${status.taskId.take(8)}... ${status.progress}%",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = BbxDim
                                )
                                Text(
                                    "Video generation can take 5-20 minutes",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = Neutral500,
                                    fontSize = 11.sp
                                )
                            }
                        }
                    }
                }
            }
        }

        // Result
        AnimatedVisibility(
            visible = resultUrl != null,
            enter = fadeIn() + scaleIn(initialScale = 0.9f),
            exit = fadeOut()
        ) {
            resultUrl?.let { url ->
                Column(modifier = Modifier.padding(top = 20.dp)) {
                    // Success banner
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(RadiusMd))
                            .background(SolidGreen.copy(alpha = 0.1f))
                            .border(1.dp, SolidGreen.copy(alpha = 0.3f), RoundedCornerShape(RadiusMd))
                            .padding(14.dp)
                    ) {
                        Column {
                            Text(
                                "Video Ready",
                                style = MaterialTheme.typography.labelLarge.copy(fontWeight = FontWeight.Bold),
                                color = SolidGreen
                            )
                            Spacer(Modifier.height(4.dp))
                            Text(
                                url,
                                style = MaterialTheme.typography.bodySmall,
                                color = BbxDim,
                                maxLines = 2
                            )
                        }
                    }
                    Spacer(Modifier.height(12.dp))

                    // Generate another button
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(RadiusLg))
                            .glassSurface(
                                shape = RoundedCornerShape(RadiusLg),
                                bg = GlassFloatingBubble
                            )
                            .clickFeedback {
                                viewModel.reset()
                                prompt = ""
                            }
                            .padding(vertical = 12.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        Text(
                            "Generate Another",
                            color = BbxAccent,
                            style = MaterialTheme.typography.labelLarge.copy(fontWeight = FontWeight.Medium)
                        )
                    }
                }
            }
        }

        Spacer(Modifier.height(180.dp))
    }
    }
}
