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
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import coil.compose.AsyncImage
import com.aiblackbox.portal.data.repository.ImageCatalogProvider
import com.aiblackbox.portal.data.repository.ImageCatalogRepository
import com.aiblackbox.portal.data.repository.ImageParamSpec
import com.aiblackbox.portal.ui.components.EmberBackdrop
import com.aiblackbox.portal.ui.components.GlassCard
import com.aiblackbox.portal.ui.voice.LabeledDropdown
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.DurationBase
import com.aiblackbox.portal.ui.theme.EaseStandard
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.GlassFloatingBubble
import com.aiblackbox.portal.ui.theme.Neutral100
import com.aiblackbox.portal.ui.theme.Neutral200
import com.aiblackbox.portal.ui.theme.Neutral300
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.PillShape
import com.aiblackbox.portal.ui.theme.RadiusLg
import com.aiblackbox.portal.ui.theme.RadiusMd
import com.aiblackbox.portal.ui.theme.RadiusXl
import com.aiblackbox.portal.ui.theme.SolidGreen
import com.aiblackbox.portal.ui.theme.glassSurface

/**
 * Provider-aware image-generation screen. The provider dropdown + param controls
 * hydrate from GET /image/catalog (the SAME source as the Portal modal — Task 8),
 * so the two surfaces stay aligned. Selecting a provider SWAPS the param controls
 * to that provider's schema (enum -> dropdown, int -> chip stepper).
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun ImageGenScreen(
    origin: String,
    modifier: Modifier = Modifier,
    viewModel: GenerationViewModel = viewModel()
) {
    val view = LocalView.current
    var prompt by remember { mutableStateOf("") }
    val state by viewModel.state.collectAsState()
    val resultUrl by viewModel.resultUrl.collectAsState()
    val error by viewModel.error.collectAsState()
    val taskStatus by viewModel.taskStatus.collectAsState()
    val providers by viewModel.imageProviders.collectAsState()
    val selectedProvider by viewModel.selectedImageProvider.collectAsState()
    val paramValues by viewModel.imageParamValues.collectAsState()

    LaunchedEffect(origin) { viewModel.initialize(origin) }

    val currentEntry: ImageCatalogProvider? =
        providers.firstOrNull { it.provider == selectedProvider }

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
            "Image Generation",
            style = MaterialTheme.typography.headlineMedium.copy(fontWeight = FontWeight.Bold),
            color = BbxWhite
        )
        Spacer(Modifier.height(4.dp))
        Text(
            currentEntry?.label ?: "Loading providers...",
            style = MaterialTheme.typography.bodySmall,
            color = Neutral500
        )
        Spacer(Modifier.height(20.dp))

        // Provider selector — only meaningful with 2+ enabled providers, but always
        // shown for parity with the Portal modal (and to surface the active provider).
        if (providers.isNotEmpty()) {
            GlassCard(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(RadiusMd)
            ) {
                Column(modifier = Modifier.padding(start = 14.dp, end = 14.dp, top = 14.dp, bottom = 4.dp)) {
                    LabeledDropdown(
                        label = "Provider",
                        options = providers.map { it.provider to (it.label.ifBlank { it.provider }) },
                        selectedId = selectedProvider,
                        onSelect = { id ->
                            viewModel.selectImageProvider(id)
                        }
                    )
                }
            }
            Spacer(Modifier.height(16.dp))
        }

        // Prompt input inside a GlassCard
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
                            "Describe the image you want to create...",
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

        // Dynamic params card — rendered from the selected provider's schema.
        // enum -> dropdown; int -> chip stepper. Controls SWAP on provider change.
        val params = currentEntry?.params ?: emptyList()
        if (params.isNotEmpty()) {
            GlassCard(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(RadiusMd)
            ) {
                Column(modifier = Modifier.padding(14.dp)) {
                    params.forEachIndexed { index, spec ->
                        ImageParamControl(
                            spec = spec,
                            value = paramValues[spec.name] ?: spec.default ?: spec.options.firstOrNull() ?: "",
                            onSelect = { v ->
                                viewModel.setImageParam(spec.name, v)
                            }
                        )
                        if (index < params.lastIndex) Spacer(Modifier.height(18.dp))
                    }
                }
            }
            Spacer(Modifier.height(20.dp))
        }

        // Generate button with press animation
        val btnInteraction = remember { MutableInteractionSource() }
        val btnPressed by btnInteraction.collectIsPressedAsState()
        val btnScale by animateFloatAsState(
            targetValue = if (btnPressed) 0.96f else 1f,
            animationSpec = tween(DurationBase, easing = EaseStandard),
            label = "btnScale"
        )
        val isGenerating = state == GenState.SUBMITTING || state == GenState.POLLING
        val btnEnabled = prompt.isNotBlank() && selectedProvider != null && !isGenerating

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
                    val provider = selectedProvider ?: return@clickFeedback
                    val intNames = params.filter { it.type == "int" }.map { it.name }.toSet()
                    viewModel.generateImageForProvider(
                        prompt = prompt,
                        provider = provider,
                        params = paramValues,
                        intParamNames = intNames
                    )
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
                        GenState.IDLE -> "Generate"
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
                            Text(
                                "Task ${status.taskId.take(8)}... ${status.progress}%",
                                style = MaterialTheme.typography.bodySmall,
                                color = BbxDim
                            )
                        }
                    }
                }
            }
        }

        // Result image
        AnimatedVisibility(
            visible = resultUrl != null,
            enter = fadeIn() + scaleIn(initialScale = 0.9f),
            exit = fadeOut()
        ) {
            resultUrl?.let { url ->
                Column(modifier = Modifier.padding(top = 20.dp)) {
                    Text(
                        "Result",
                        style = MaterialTheme.typography.labelLarge.copy(fontWeight = FontWeight.Bold),
                        color = SolidGreen
                    )
                    Spacer(Modifier.height(10.dp))
                    GlassCard(
                        modifier = Modifier.fillMaxWidth(),
                        shape = RoundedCornerShape(RadiusXl)
                    ) {
                        AsyncImage(
                            model = url,
                            contentDescription = "Generated image",
                            modifier = Modifier
                                .fillMaxWidth()
                                .clip(RoundedCornerShape(RadiusXl)),
                            contentScale = ContentScale.FillWidth
                        )
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

/**
 * One dynamic param control rendered from a catalog [ImageParamSpec].
 *   enum -> [LabeledDropdown]; int -> a chip stepper across [min]..[max].
 * The label uses the friendly name from [ImageCatalogRepository.IMAGE_PARAM_LABELS].
 */
@Composable
private fun ImageParamControl(
    spec: ImageParamSpec,
    value: String,
    onSelect: (String) -> Unit,
) {
    val label = ImageCatalogRepository.IMAGE_PARAM_LABELS[spec.name] ?: spec.name

    if (spec.type == "enum") {
        LabeledDropdown(
            label = label,
            options = spec.options.map { it to it },
            selectedId = value,
            onSelect = onSelect
        )
    } else {
        // int (or any non-enum): chip stepper across min..max (default 1..4)
        Text(
            label,
            style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Medium),
            color = BbxDim
        )
        Spacer(Modifier.height(10.dp))
        val lo = spec.min ?: 1
        val hi = spec.max ?: 4
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            (lo..hi).forEach { n ->
                val sel = value == n.toString()
                val interactionSource = remember { MutableInteractionSource() }
                val pressed by interactionSource.collectIsPressedAsState()
                val chipScale by animateFloatAsState(
                    targetValue = if (pressed) 0.92f else 1f,
                    animationSpec = tween(DurationBase, easing = EaseStandard),
                    label = "countScale"
                )
                val chipBg by animateColorAsState(
                    targetValue = if (sel) BbxAccent.copy(alpha = 0.18f) else Neutral200,
                    animationSpec = tween(DurationBase, easing = EaseStandard),
                    label = "countBg"
                )
                val chipBorder by animateColorAsState(
                    targetValue = if (sel) BbxAccent.copy(alpha = 0.5f) else Neutral300,
                    animationSpec = tween(DurationBase, easing = EaseStandard),
                    label = "countBorder"
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
                        ) { onSelect(n.toString()) }
                        .padding(horizontal = 16.dp, vertical = 8.dp)
                ) {
                    Text(
                        "$n",
                        style = MaterialTheme.typography.labelMedium.copy(
                            fontWeight = if (sel) FontWeight.Bold else FontWeight.Normal
                        ),
                        color = if (sel) BbxAccent else Neutral500
                    )
                }
            }
        }
    }
}
