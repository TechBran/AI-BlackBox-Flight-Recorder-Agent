package com.aiblackbox.portal.ui.chat

import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInHorizontally
import androidx.compose.animation.slideOutHorizontally
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import com.aiblackbox.portal.ui.feedback.clickFeedback
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
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
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.draw.scale
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.RadiusXl
import com.aiblackbox.portal.ui.theme.glassSurface
import kotlinx.coroutines.delay

// =============================================================================
// HomeScreen — aligned with Portal .home-screen
//
// Portal structure:
//   .home-bg-grid (subtle animated grid)
//   .home-header
//     .home-logo (core pulse + 3 spinning rings)
//     .home-title "AI BlackBox" (42px, 800 weight, letter-spacing 2px)
//     .home-subtitle "Flight Recorder Portal" (18px, accent, uppercase, ls 4px)
//     .home-tagline "Immutable Memory • Multimodal Intelligence • Real-time Voice"
//   .home-features (carousel, 9 cards, 4s interval)
//     .feature-card (glass bg, accent border, icon + title + description)
//   .carousel-dots (10px dots, active = accent + scale 1.3)
//   .home-quickstart (pill with lightbulb + tip text)
// =============================================================================

// 9 feature cards matching Portal index.html
private data class FeatureCard(
    val icon: String,
    val title: String,
    val description: String
)

private val FEATURES = listOf(
    FeatureCard("\uD83E\uDDE0", "Memory System",
        "Immutable conversation ledger with cryptographic verification. Every interaction is preserved forever."),
    FeatureCard("\uD83E\uDD16", "AI Agents",
        "Autonomous agents powered by Claude Code CLI, Gemini, and Claude. Execute complex tasks with persistent memory."),
    FeatureCard("\uD83D\uDD0D", "Semantic Search",
        "Find anything in your history using natural language. AI-powered embeddings understand meaning, not just keywords."),
    FeatureCard("\uD83C\uDF99\uFE0F", "Real-time Voice",
        "Live voice conversations with Gemini Live, GPT Realtime, and Grok Live. Talk naturally with AI."),
    FeatureCard("\uD83D\uDCF1", "App Registry",
        "Launch and manage your own apps via reverse proxy. Accessible anywhere through Tailscale integration."),
    FeatureCard("\uD83C\uDFA8", "Image & Video",
        "Generate images with Imagen and videos with Veo 3.1. Analyze visual content with multimodal AI vision."),
    FeatureCard("\uD83C\uDFB5", "Lyria Music",
        "Generate original music from text prompts. Create 30-second compositions with Google's Lyria model."),
    FeatureCard("\uD83D\uDD0A", "Text-to-Speech",
        "30+ premium voices from Gemini Pro TTS. Natural prosody with emotional expression support."),
    FeatureCard("\uD83D\uDCBE", "Checkpoints",
        "Save conversation milestones manually. Compress context for long-running sessions without losing history."),
)

private const val CAROUSEL_INTERVAL_MS = 4000L

@Composable
fun HomeScreen(modifier: Modifier = Modifier) {
    // Carousel state
    var activeIndex by remember { mutableIntStateOf(0) }

    // Auto-rotate every 4 seconds (matches Portal CAROUSEL_INTERVAL_MS)
    LaunchedEffect(Unit) {
        while (true) {
            delay(CAROUSEL_INTERVAL_MS)
            activeIndex = (activeIndex + 1) % FEATURES.size
        }
    }

    // No scroll — everything fits on screen
    Column(
        modifier = modifier
            .fillMaxSize()
            .padding(horizontal = 24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center
    ) {
        // ── Animated Logo (compact) ──
        AnimatedLogo(modifier = Modifier.size(60.dp))

        Spacer(Modifier.height(14.dp))

        // ── Title ──
        Text(
            text = "AI BlackBox",
            style = MaterialTheme.typography.headlineLarge.copy(
                fontWeight = FontWeight.ExtraBold,
                color = BbxWhite,
                letterSpacing = 2.sp
            )
        )

        // ── Subtitle ──
        Text(
            text = "FLIGHT RECORDER PORTAL",
            style = MaterialTheme.typography.labelLarge.copy(
                color = BbxAccent,
                fontWeight = FontWeight.SemiBold,
                letterSpacing = 4.sp
            ),
            modifier = Modifier.padding(top = 6.dp)
        )

        // ── Tagline ──
        Text(
            text = "Immutable Memory \u2022 Multimodal Intelligence \u2022 Real-time Voice",
            style = MaterialTheme.typography.bodySmall.copy(
                color = BbxWhite.copy(alpha = 0.5f),
                letterSpacing = 0.5.sp,
                fontSize = 11.sp
            ),
            textAlign = TextAlign.Center,
            modifier = Modifier.padding(top = 8.dp)
        )

        Spacer(Modifier.height(24.dp))

        // ── Feature Carousel — no box, just text on black ──
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .height(80.dp),
            contentAlignment = Alignment.Center
        ) {
            AnimatedContent(
                targetState = activeIndex,
                transitionSpec = {
                    (fadeIn(tween(500)) + slideInHorizontally(tween(500)) { it / 3 }) togetherWith
                            (fadeOut(tween(500)) + slideOutHorizontally(tween(500)) { -it / 3 })
                },
                label = "carousel"
            ) { index ->
                val feature = FEATURES[index]
                // Minimal card — just icon + title + description, no border/glass
                Column(
                    horizontalAlignment = Alignment.CenterHorizontally,
                    modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp)
                ) {
                    Text(feature.icon, fontSize = 24.sp)
                    Spacer(Modifier.height(6.dp))
                    Text(
                        feature.title,
                        style = MaterialTheme.typography.titleSmall.copy(
                            fontWeight = FontWeight.SemiBold, color = BbxWhite
                        )
                    )
                    Spacer(Modifier.height(4.dp))
                    Text(
                        feature.description,
                        style = MaterialTheme.typography.bodySmall.copy(
                            color = BbxWhite.copy(alpha = 0.5f),
                            fontSize = 12.sp, lineHeight = 16.sp
                        ),
                        textAlign = TextAlign.Center,
                        maxLines = 2
                    )
                }
            }
        }

        Spacer(Modifier.height(12.dp))

        // ── Carousel Dots (smaller) ──
        Row(
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            FEATURES.forEachIndexed { index, _ ->
                val isActive = index == activeIndex
                Box(
                    modifier = Modifier
                        .size(if (isActive) 8.dp else 6.dp)
                        .clip(CircleShape)
                        .background(
                            if (isActive) BbxAccent
                            else BbxAccent.copy(alpha = 0.25f)
                        )
                        .clickFeedback { activeIndex = index }
                )
            }
        }

        Spacer(Modifier.height(14.dp))

        // ── Quick Start Tip ──
        Text(
            text = "Type a message below to begin",
            style = MaterialTheme.typography.bodySmall.copy(
                color = Neutral500,
                fontSize = 11.sp,
                letterSpacing = 0.5.sp
            ),
            textAlign = TextAlign.Center
        )
    }
}

// =============================================================================
// Animated Logo — Canvas with pulsing core + 3 spinning rings
// Matches Portal .logo-core + .logo-ring-1/2/3
// =============================================================================
@Composable
private fun AnimatedLogo(modifier: Modifier = Modifier) {
    val transition = rememberInfiniteTransition(label = "logo")

    // Core pulse (matches Portal core-pulse: scale 1.0→1.1)
    val coreScale by transition.animateFloat(
        initialValue = 1f,
        targetValue = 1.1f,
        animationSpec = infiniteRepeatable(tween(2000), RepeatMode.Reverse),
        label = "coreScale"
    )

    // Ring rotations (matches Portal ring-spin: 8s, 12s reverse, 16s)
    val ring1Angle by transition.animateFloat(
        initialValue = 0f, targetValue = 360f,
        animationSpec = infiniteRepeatable(tween(8000, easing = LinearEasing)),
        label = "ring1"
    )
    val ring2Angle by transition.animateFloat(
        initialValue = 360f, targetValue = 0f, // reverse
        animationSpec = infiniteRepeatable(tween(12000, easing = LinearEasing)),
        label = "ring2"
    )
    val ring3Angle by transition.animateFloat(
        initialValue = 0f, targetValue = 360f,
        animationSpec = infiniteRepeatable(tween(16000, easing = LinearEasing)),
        label = "ring3"
    )

    Canvas(modifier = modifier) {
        val center = Offset(size.width / 2, size.height / 2)
        val unit = size.minDimension / 2f // proportional to canvas size

        // Core (glowing red circle)
        drawCircle(color = BbxAccent.copy(alpha = 0.3f), radius = unit * 0.6f * coreScale, center = center)
        drawCircle(color = BbxAccent, radius = unit * 0.3f * coreScale, center = center)

        // Ring 1 (solid, 8s spin)
        rotate(ring1Angle, center) {
            drawCircle(color = BbxAccent.copy(alpha = 0.3f), radius = unit * 0.55f, center = center, style = Stroke(width = 1.5.dp.toPx()))
        }

        // Ring 2 (dashed arcs, 12s reverse)
        rotate(ring2Angle, center) {
            val r2 = unit * 0.75f
            for (i in 0 until 8) {
                drawArc(
                    color = BbxAccent.copy(alpha = 0.25f), startAngle = i * 45f, sweepAngle = 25f, useCenter = false,
                    topLeft = Offset(center.x - r2, center.y - r2),
                    size = androidx.compose.ui.geometry.Size(r2 * 2, r2 * 2),
                    style = Stroke(width = 1.5.dp.toPx())
                )
            }
        }

        // Ring 3 (subtle, 16s spin)
        rotate(ring3Angle, center) {
            drawCircle(color = BbxAccent.copy(alpha = 0.15f), radius = unit * 0.95f, center = center, style = Stroke(width = 1.dp.toPx()))
        }
    }
}

// =============================================================================
// Feature Card — matches Portal .feature-card
// Glass bg, accent border, icon + title + description
// =============================================================================
@Composable
private fun FeatureCardView(feature: FeatureCard) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .glassSurface(
                shape = RoundedCornerShape(RadiusXl),
                bg = Color(0xCC141414), // rgba(20,20,20,0.8)
                elevation = 4.dp,
            )
            .border(1.dp, BbxAccent.copy(alpha = 0.2f), RoundedCornerShape(RadiusXl))
            .padding(horizontal = 32.dp, vertical = 28.dp),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        // Icon (matches Portal .feature-icon: 36px → 32sp for mobile)
        Text(
            text = feature.icon,
            fontSize = 32.sp
        )

        Spacer(Modifier.height(12.dp))

        // Title (matches Portal .feature-card h3: 18px, 700 weight)
        Text(
            text = feature.title,
            style = MaterialTheme.typography.titleMedium.copy(
                fontWeight = FontWeight.Bold,
                color = BbxWhite,
                fontSize = 18.sp
            )
        )

        Spacer(Modifier.height(10.dp))

        // Description (matches Portal .feature-card p: 13px, line-height 1.5, 70% white)
        Text(
            text = feature.description,
            style = MaterialTheme.typography.bodySmall.copy(
                color = BbxWhite.copy(alpha = 0.7f),
                fontSize = 13.sp,
                lineHeight = 20.sp
            ),
            textAlign = TextAlign.Center
        )
    }
}
