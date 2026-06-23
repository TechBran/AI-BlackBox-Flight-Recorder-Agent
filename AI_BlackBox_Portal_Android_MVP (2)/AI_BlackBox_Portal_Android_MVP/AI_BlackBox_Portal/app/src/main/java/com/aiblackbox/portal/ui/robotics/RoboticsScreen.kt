package com.aiblackbox.portal.ui.robotics

import android.app.Application
import android.graphics.BitmapFactory
import android.util.Base64
import android.util.Log
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import com.aiblackbox.portal.ui.feedback.clickFeedback
import com.aiblackbox.portal.ui.feedback.rememberPressFeedback
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
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
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
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
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.ui.components.MarkdownText
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

// =============================================================================
// Colors
// =============================================================================

private val RobotGreen = Color(0xFF4ADE80)
private val RobotGreenDim = Color(0xFF22C55E)
private val RobotGreenBg = Color(0x1A4ADE80)
private val RobotGreenBorder = Color(0x734ADE80)
private val RobotSurface = Color(0xFF1A1A1A)
private val RobotDarkBg = Color(0xFF0A0A0A)
private val RobotYellow = Color(0xFFFACC15)
private val RobotRed = Color(0xFFF87171)

// =============================================================================
// Camera options
// =============================================================================

private val CAMERAS = listOf(
    "pantilt" to "Pan-Tilt",
    "oakd" to "OAK-D",
    "yolo_pantilt" to "YOLO (PT)",
    "yolo_oakd" to "YOLO (OAK)",
)

// =============================================================================
// ViewModel
// =============================================================================

class RoboticsViewModel(application: Application) : AndroidViewModel(application) {
    companion object {
        private const val TAG = "RoboticsVM"
        private const val POLL_MS = 2000L
    }

    private var api: BlackBoxApi? = null
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    private val _cameraBitmap = MutableStateFlow<ImageBitmap?>(null)
    val cameraBitmap: StateFlow<ImageBitmap?> = _cameraBitmap.asStateFlow()

    private val _frameInfo = MutableStateFlow("")
    val frameInfo: StateFlow<String> = _frameInfo.asStateFlow()

    private val _robotHealthy = MutableStateFlow(false)
    val robotHealthy: StateFlow<Boolean> = _robotHealthy.asStateFlow()

    private val _isPolling = MutableStateFlow(false)
    val isPolling: StateFlow<Boolean> = _isPolling.asStateFlow()

    private val _selectedCamera = MutableStateFlow("yolo_oakd")
    val selectedCamera: StateFlow<String> = _selectedCamera.asStateFlow()

    private val _statusText = MutableStateFlow("Connecting...")
    val statusText: StateFlow<String> = _statusText.asStateFlow()

    fun initialize(origin: String) {
        if (origin.isNotBlank() && api == null) {
            api = BlackBoxApi(origin)
            checkHealth()
        }
    }

    fun setCamera(camera: String) {
        _selectedCamera.value = camera
        if (_isPolling.value) fetchFrame()
    }

    fun startPolling() {
        if (_isPolling.value) return
        _isPolling.value = true
        _statusText.value = "Live"
        viewModelScope.launch {
            while (_isPolling.value) {
                fetchFrame()
                delay(POLL_MS)
            }
        }
    }

    fun stopPolling() {
        _isPolling.value = false
        _statusText.value = "Paused"
    }

    fun refreshOnce() {
        fetchFrame()
    }

    private fun checkHealth() {
        viewModelScope.launch {
            try {
                val response = api?.get("/robotics/health") ?: return@launch
                val obj = json.parseToJsonElement(response).jsonObject
                _robotHealthy.value = obj["healthy"]?.jsonPrimitive?.boolean == true
                _statusText.value = if (_robotHealthy.value) "Connected" else "Robot Offline"
                if (_robotHealthy.value) startPolling()
            } catch (e: Exception) {
                _robotHealthy.value = false
                _statusText.value = "Robot Offline"
                Log.w(TAG, "Health check failed: ${e.message}")
            }
        }
    }

    private fun fetchFrame() {
        viewModelScope.launch {
            try {
                val camera = _selectedCamera.value
                val response = api?.get("/robotics/camera/b64?camera=$camera") ?: return@launch
                val obj = json.parseToJsonElement(response).jsonObject
                val imageB64 = obj["image"]?.jsonPrimitive?.content ?: return@launch
                val size = obj["size"]?.jsonPrimitive?.int ?: 0

                val bytes = Base64.decode(imageB64, Base64.DEFAULT)
                val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
                if (bitmap != null) {
                    _cameraBitmap.value = bitmap.asImageBitmap()
                    _frameInfo.value = "$camera  \u2022  ${size / 1024}KB"
                    _robotHealthy.value = true
                    if (_statusText.value != "Live") _statusText.value = "Live"
                }
            } catch (e: Exception) {
                Log.w(TAG, "Frame fetch failed: ${e.message}")
                _statusText.value = "Connection Lost"
            }
        }
    }

    override fun onCleared() {
        super.onCleared()
        stopPolling()
    }
}

// =============================================================================
// Screen Composable
// =============================================================================

@Composable
fun RoboticsScreen(
    origin: String,
    model: String = "",
    erStatus: String = "offline",
    erReasoning: String = "",
    erCameraFrame: String? = null,
    onModelChange: (String) -> Unit = {},
    onCameraChange: (String) -> Unit = {},
    modifier: Modifier = Modifier,
    viewModel: RoboticsViewModel = viewModel()
) {
    val cameraBitmap by viewModel.cameraBitmap.collectAsState()
    val frameInfo by viewModel.frameInfo.collectAsState()
    val robotHealthy by viewModel.robotHealthy.collectAsState()
    val isPolling by viewModel.isPolling.collectAsState()
    val selectedCamera by viewModel.selectedCamera.collectAsState()
    val statusText by viewModel.statusText.collectAsState()

    // Decode SSE-driven frame if local polling has no frame yet
    val sseCameraBitmap = remember(erCameraFrame) {
        erCameraFrame?.let { b64 ->
            try {
                val bytes = Base64.decode(b64, Base64.DEFAULT)
                BitmapFactory.decodeByteArray(bytes, 0, bytes.size)?.asImageBitmap()
            } catch (_: Exception) { null }
        }
    }

    val displayBitmap = cameraBitmap ?: sseCameraBitmap

    LaunchedEffect(origin) {
        viewModel.initialize(origin)
    }

    // Pulsing glow animation for connected state
    val infiniteTransition = rememberInfiniteTransition(label = "glow")
    val glowAlpha by infiniteTransition.animateFloat(
        initialValue = 0.3f,
        targetValue = 0.7f,
        animationSpec = infiniteRepeatable(
            animation = tween(1200, easing = LinearEasing),
            repeatMode = RepeatMode.Reverse
        ),
        label = "glowAlpha"
    )

    val statusColor by animateColorAsState(
        targetValue = when {
            isPolling && robotHealthy -> RobotGreen
            erStatus == "reasoning" -> RobotYellow
            robotHealthy -> RobotGreenDim
            else -> RobotRed
        },
        label = "statusColor"
    )

    // Collapsible state for the camera feed
    var cameraExpanded by remember { mutableStateOf(true) }

    Surface(
        modifier = modifier.fillMaxSize(),
        color = Color(0xFF0E0E10)
    ) {
        Column(
            modifier = Modifier.fillMaxSize()
        ) {
            // ══════════════════════════════════════════════════
            // PINNED SECTION (does not scroll)
            // ══════════════════════════════════════════════════

            Column(modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp)) {
                // ── Header (always visible, tap to collapse/expand camera) ──
                RoboticsHeader(
                    statusText = statusText,
                    statusColor = statusColor,
                    isPolling = isPolling,
                    selectedCamera = selectedCamera,
                    cameraExpanded = cameraExpanded,
                    onToggleExpand = { cameraExpanded = !cameraExpanded },
                    onCameraChange = { camera ->
                        viewModel.setCamera(camera)
                        onCameraChange(camera)
                    }
                )

                // ── Camera Feed (collapsible) ──
                androidx.compose.animation.AnimatedVisibility(
                    visible = cameraExpanded,
                    enter = androidx.compose.animation.expandVertically() + androidx.compose.animation.fadeIn(),
                    exit = androidx.compose.animation.shrinkVertically() + androidx.compose.animation.fadeOut()
                ) {
                    Column {
                        Spacer(modifier = Modifier.height(8.dp))
                        CameraFeed(
                            bitmap = displayBitmap,
                            frameInfo = frameInfo,
                            isLive = isPolling && robotHealthy,
                            glowAlpha = glowAlpha,
                            statusColor = statusColor
                        )
                    }
                }
            }

            // ══════════════════════════════════════════════════
            // SCROLLABLE SECTION (below pinned camera)
            // ══════════════════════════════════════════════════

            Column(
                modifier = Modifier
                    .weight(1f)
                    .verticalScroll(rememberScrollState())
                    .padding(horizontal = 12.dp)
                    .padding(bottom = 80.dp)
            ) {
                Spacer(modifier = Modifier.height(8.dp))

                // ── ER Reasoning Panel ──
                if (erReasoning.isNotBlank()) {
                    ReasoningPanel(text = erReasoning, erStatus = erStatus)
                    Spacer(modifier = Modifier.height(10.dp))
                }

                // ── Controls ──
                ControlsRow(
                    isPolling = isPolling,
                    robotHealthy = robotHealthy,
                    onTogglePolling = {
                        if (isPolling) viewModel.stopPolling() else viewModel.startPolling()
                    },
                    onRefresh = { viewModel.refreshOnce() }
                )

                Spacer(modifier = Modifier.height(24.dp))
            }
        }
    }
}

// =============================================================================
// Header
// =============================================================================

@Composable
private fun RoboticsHeader(
    statusText: String,
    statusColor: Color,
    isPolling: Boolean,
    selectedCamera: String,
    cameraExpanded: Boolean,
    onToggleExpand: () -> Unit,
    onCameraChange: (String) -> Unit
) {
    Card(
        colors = CardDefaults.cardColors(containerColor = RobotSurface),
        shape = RoundedCornerShape(12.dp)
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .clickFeedback { onToggleExpand() }
                .padding(horizontal = 14.dp, vertical = 10.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            // Status dot
            Box(
                modifier = Modifier
                    .size(10.dp)
                    .clip(CircleShape)
                    .background(statusColor)
            )

            // Title
            Text(
                text = "Robot Camera",
                style = MaterialTheme.typography.titleSmall,
                color = RobotGreen,
                fontWeight = FontWeight.SemiBold
            )

            // Expand/collapse arrow
            Text(
                text = if (cameraExpanded) "\u25BC" else "\u25B6",
                fontSize = 10.sp,
                color = RobotGreen.copy(alpha = 0.6f)
            )

            // Status
            Text(
                text = statusText,
                style = MaterialTheme.typography.bodySmall,
                color = statusColor.copy(alpha = 0.8f),
                modifier = Modifier.weight(1f)
            )

            // Camera selector chips
            CameraSelector(
                selected = selectedCamera,
                onChange = onCameraChange
            )
        }
    }
}

@Composable
private fun CameraSelector(
    selected: String,
    onChange: (String) -> Unit
) {
    var expanded by remember { mutableStateOf(false) }
    val displayName = CAMERAS.find { it.first == selected }?.second ?: selected
    val feedback = rememberPressFeedback()

    Box {
        FilterChip(
            selected = true,
            onClick = { feedback(); expanded = true },
            label = {
                Text(
                    text = displayName,
                    fontSize = 11.sp,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
            },
            colors = FilterChipDefaults.filterChipColors(
                selectedContainerColor = RobotGreenBg,
                selectedLabelColor = RobotGreen
            ),
            border = FilterChipDefaults.filterChipBorder(
                selectedBorderColor = RobotGreenBorder,
                enabled = true,
                selected = true,
                borderWidth = 1.dp
            )
        )

        DropdownMenu(
            expanded = expanded,
            onDismissRequest = { expanded = false }
        ) {
            CAMERAS.forEach { (id, name) ->
                DropdownMenuItem(
                    text = {
                        Text(
                            name,
                            color = if (id == selected) RobotGreen else Color.White
                        )
                    },
                    onClick = {
                        feedback()
                        onChange(id)
                        expanded = false
                    }
                )
            }
        }
    }
}

// =============================================================================
// Camera Feed
// =============================================================================

@Composable
private fun CameraFeed(
    bitmap: ImageBitmap?,
    frameInfo: String,
    isLive: Boolean,
    glowAlpha: Float,
    statusColor: Color
) {
    val borderColor = if (isLive) statusColor.copy(alpha = glowAlpha) else Color.Transparent

    Card(
        colors = CardDefaults.cardColors(containerColor = RobotDarkBg),
        shape = RoundedCornerShape(12.dp),
        modifier = Modifier
            .fillMaxWidth()
            .border(
                width = 1.5.dp,
                color = borderColor,
                shape = RoundedCornerShape(12.dp)
            )
    ) {
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .aspectRatio(4f / 3f),
            contentAlignment = Alignment.Center
        ) {
            if (bitmap != null) {
                Image(
                    bitmap = bitmap,
                    contentDescription = "Robot camera feed",
                    modifier = Modifier
                        .fillMaxSize()
                        .clip(RoundedCornerShape(12.dp)),
                    contentScale = ContentScale.Fit
                )

                // Frame info overlay
                if (frameInfo.isNotBlank()) {
                    Text(
                        text = frameInfo,
                        fontSize = 10.sp,
                        color = Color.White.copy(alpha = 0.6f),
                        modifier = Modifier
                            .align(Alignment.BottomEnd)
                            .padding(8.dp)
                            .background(
                                Color.Black.copy(alpha = 0.5f),
                                RoundedCornerShape(4.dp)
                            )
                            .padding(horizontal = 6.dp, vertical = 2.dp)
                    )
                }

                // Live indicator
                if (isLive) {
                    Row(
                        modifier = Modifier
                            .align(Alignment.TopStart)
                            .padding(8.dp)
                            .background(
                                Color.Black.copy(alpha = 0.6f),
                                RoundedCornerShape(4.dp)
                            )
                            .padding(horizontal = 6.dp, vertical = 2.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(4.dp)
                    ) {
                        Box(
                            modifier = Modifier
                                .size(6.dp)
                                .clip(CircleShape)
                                .background(RobotGreen)
                        )
                        Text(
                            text = "LIVE",
                            fontSize = 9.sp,
                            fontWeight = FontWeight.Bold,
                            color = RobotGreen,
                            letterSpacing = 1.sp
                        )
                    }
                }
            } else {
                // Placeholder
                Column(
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.Center
                ) {
                    Text(
                        text = "\uD83E\uDD16",
                        fontSize = 40.sp
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        text = "Connecting to robot camera...",
                        style = MaterialTheme.typography.bodyMedium,
                        color = Color.White.copy(alpha = 0.4f)
                    )
                }
            }
        }
    }
}

// =============================================================================
// ER Reasoning Panel
// =============================================================================

@Composable
private fun ReasoningPanel(text: String, erStatus: String) {
    Card(
        colors = CardDefaults.cardColors(containerColor = RobotSurface),
        shape = RoundedCornerShape(12.dp)
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(12.dp)
        ) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(6.dp)
            ) {
                Box(
                    modifier = Modifier
                        .width(3.dp)
                        .height(14.dp)
                        .background(RobotGreen, RoundedCornerShape(2.dp))
                )
                Text(
                    text = "ER Reasoning",
                    style = MaterialTheme.typography.labelMedium,
                    color = RobotGreen,
                    fontWeight = FontWeight.SemiBold,
                    letterSpacing = 0.5.sp
                )
                if (erStatus == "reasoning") {
                    Text(
                        text = "thinking...",
                        style = MaterialTheme.typography.labelSmall,
                        color = RobotYellow.copy(alpha = 0.7f)
                    )
                }
            }

            Spacer(modifier = Modifier.height(8.dp))

            MarkdownText(
                content = text,
                modifier = Modifier
                    .fillMaxWidth()
                    .background(RobotGreenBg, RoundedCornerShape(8.dp))
                    .padding(10.dp)
            )
        }
    }
}

// =============================================================================
// Controls Row
// =============================================================================

@Composable
private fun ControlsRow(
    isPolling: Boolean,
    robotHealthy: Boolean,
    onTogglePolling: () -> Unit,
    onRefresh: () -> Unit
) {
    val feedback = rememberPressFeedback()
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        Button(
            onClick = { feedback(); onTogglePolling() },
            colors = ButtonDefaults.buttonColors(
                containerColor = if (isPolling) RobotRed.copy(alpha = 0.2f) else RobotGreenBg,
                contentColor = if (isPolling) RobotRed else RobotGreen
            ),
            shape = RoundedCornerShape(8.dp),
            modifier = Modifier.weight(1f)
        ) {
            Text(
                text = if (isPolling) "Pause" else "Resume",
                fontWeight = FontWeight.SemiBold
            )
        }

        OutlinedButton(
            onClick = { feedback(); onRefresh() },
            shape = RoundedCornerShape(8.dp),
            modifier = Modifier.weight(1f),
            colors = ButtonDefaults.outlinedButtonColors(
                contentColor = Color.White.copy(alpha = 0.7f)
            )
        ) {
            Text("Refresh")
        }
    }
}
