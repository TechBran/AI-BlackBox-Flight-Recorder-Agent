package com.aiblackbox.portal.ui.computeruse

import android.app.Application
import android.graphics.BitmapFactory
import android.view.HapticFeedbackConstants
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.DropdownMenuItem
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
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.util.Constants
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.DurationBase
import com.aiblackbox.portal.ui.theme.DurationFast
import com.aiblackbox.portal.ui.theme.EaseStandard
import com.aiblackbox.portal.ui.theme.Neutral100
import com.aiblackbox.portal.ui.theme.Neutral150
import com.aiblackbox.portal.ui.theme.Neutral200
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.Neutral700
import com.aiblackbox.portal.ui.theme.PillShape
import com.aiblackbox.portal.ui.theme.RadiusMd
import com.aiblackbox.portal.ui.theme.RadiusSm
import com.aiblackbox.portal.ui.theme.SolidGreen
import com.aiblackbox.portal.ui.theme.glassSurface
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put

// =============================================================================
// CU display coordinate space — matches Portal's cu-interact.js
// =============================================================================
private const val DISPLAY_WIDTH = 1280
private const val DISPLAY_HEIGHT = 720
private const val POLL_MS = 2000L

// CU purple accent — matches Portal's #6c3bd1 / #8b5cf6
private val CuPurple = Color(0xFF8B5CF6)
private val CuPurpleDark = Color(0xFF6C3BD1)
private val CuPurpleDim = Color(0xFFC4B5FD)
private val CuPurpleBg = Color(0x146C3BD1)       // ~8% alpha
private val CuPurpleBorder = Color(0x408B5CF6)    // ~25% alpha

// CU status colors — from Portal _browser.css
private val CuGreen = Color(0xFF4ADE80)
private val CuGreenGlow = Color(0x804ADE80)       // 50% for glow
private val CuRed = Color(0xFFF87171)
private val CuOrange = Color(0xFFF97316)

// =============================================================================
// Device data model
// =============================================================================
data class CuDevice(
    val id: String,
    val name: String,
    val protocol: String,
    val status: String,
)

// =============================================================================
// ViewModel
// =============================================================================

class CuViewModel(application: Application) : AndroidViewModel(application) {
    private var api: BlackBoxApi? = null
    private var baseUrl: String = ""

    // Screenshot as raw bytes — always keeps the last successful capture
    private val _screenshotBytes = MutableStateFlow<ByteArray?>(null)
    val screenshotBytes: StateFlow<ByteArray?> = _screenshotBytes.asStateFlow()
    private var fetchingScreenshot = false

    private val _isPolling = MutableStateFlow(false)
    val isPolling: StateFlow<Boolean> = _isPolling.asStateFlow()

    // Status feedback
    private val _statusText = MutableStateFlow("Tap Start to begin")
    val statusText: StateFlow<String> = _statusText.asStateFlow()

    // Device list + selection
    private val _devices = MutableStateFlow<List<CuDevice>>(emptyList())
    val devices: StateFlow<List<CuDevice>> = _devices.asStateFlow()

    private val _selectedDeviceId = MutableStateFlow("blackbox")
    val selectedDeviceId: StateFlow<String> = _selectedDeviceId.asStateFlow()

    // Loading indicator for actions
    private val _actionInFlight = MutableStateFlow(false)
    val actionInFlight: StateFlow<Boolean> = _actionInFlight.asStateFlow()

    // Provider selection (Anthropic or Gemini)
    private val _selectedProvider = MutableStateFlow("anthropic")
    val selectedProvider: StateFlow<String> = _selectedProvider.asStateFlow()

    fun initialize(origin: String) {
        if (origin.isBlank() || api != null) return
        baseUrl = origin
        api = BlackBoxApi(origin)
        fetchDevices()
        // Load initial screenshot immediately so the viewer isn't blank
        refreshScreenshot()
    }

    fun selectDevice(deviceId: String) {
        _selectedDeviceId.value = deviceId
        // Clear current screenshot so user sees loading state (not stale old device)
        _screenshotBytes.value = null
        _statusText.value = "Connecting to ${deviceId}..."
        // Force-refresh even if a poll is in-flight (cancel the guard)
        fetchingScreenshot = false
        refreshScreenshot()
    }

    fun selectProvider(provider: String) {
        _selectedProvider.value = provider
        fetchDevices() // Re-fetch with new provider filter
    }

    // ── Device list from /devices/ API ──
    fun fetchDevices() {
        val api = api ?: return
        val providerIsGemini = _selectedProvider.value == "google"
        viewModelScope.launch {
            try {
                val raw = api.get("/devices/")
                val json = Json { ignoreUnknownKeys = true }
                val root = json.parseToJsonElement(raw).jsonObject
                val arr = root["devices"]?.jsonArray ?: return@launch
                val list = arr.mapNotNull { el ->
                    val obj = el.jsonObject
                    val id = obj["id"]?.jsonPrimitive?.content ?: return@mapNotNull null
                    val name = obj["name"]?.jsonPrimitive?.content ?: id
                    val protocol = obj["protocol"]?.jsonPrimitive?.content ?: "local"
                    val status = obj["status"]?.jsonPrimitive?.content ?: "unknown"
                    // Anthropic: LOCAL + VNC only. Gemini: ALL (including ADB)
                    if (!providerIsGemini && protocol != "local" && protocol != "vnc") return@mapNotNull null
                    CuDevice(id, name, protocol, status)
                }
                val hasBlackbox = list.any { it.id == "blackbox" }
                _devices.value = if (hasBlackbox) list
                else listOf(CuDevice("blackbox", "BlackBox (Local)", "local", "online")) + list
            } catch (_: Exception) {
                _devices.value = listOf(
                    CuDevice("blackbox", "BlackBox (Local)", "local", "online")
                )
            }
        }
    }

    // ── Polling — fetches screenshot bytes every POLL_MS ──
    fun startPolling() {
        if (_isPolling.value) return
        _isPolling.value = true
        _statusText.value = "Live"
        viewModelScope.launch {
            while (_isPolling.value) {
                refreshScreenshot()
                delay(POLL_MS)
            }
        }
    }

    fun stopPolling() {
        _isPolling.value = false
        _statusText.value = "Stopped"
    }

    // ── Click action — POST /browser/click (matches Portal cu-interact.js) ──
    fun click(x: Int, y: Int) {
        val api = api ?: return
        _statusText.value = "Clicked at $x, $y"
        viewModelScope.launch {
            _actionInFlight.value = true
            try {
                api.post(
                    "/browser/click",
                    buildJsonObject {
                        put("x", x)
                        put("y", y)
                        put("button", "left")
                        put("device_id", _selectedDeviceId.value)
                    }.toString()
                )
                // Wait for action to take effect, then grab fresh screenshot
                delay(350)
                refreshScreenshot()
            } catch (e: Exception) {
                _statusText.value = "Click failed: ${e.message?.take(40) ?: "unknown"}"
            } finally {
                _actionInFlight.value = false
            }
        }
    }

    // ── Type text — POST /browser/type ──
    fun typeText(text: String) {
        val api = api ?: return
        _statusText.value = "Typed: ${text.take(20)}"
        viewModelScope.launch {
            _actionInFlight.value = true
            try {
                api.post(
                    "/browser/type",
                    buildJsonObject {
                        put("text", text)
                        put("device_id", _selectedDeviceId.value)
                    }.toString()
                )
                delay(350)
                refreshScreenshot()
            } catch (e: Exception) {
                _statusText.value = "Type failed: ${e.message?.take(40) ?: "unknown"}"
            } finally {
                _actionInFlight.value = false
            }
        }
    }

    // ── Key action — POST /browser/key ──
    fun sendKey(key: String) {
        val api = api ?: return
        _statusText.value = "Key: $key"
        viewModelScope.launch {
            _actionInFlight.value = true
            try {
                api.post(
                    "/browser/key",
                    buildJsonObject {
                        put("key", key)
                        put("device_id", _selectedDeviceId.value)
                    }.toString()
                )
                delay(350)
                refreshScreenshot()
            } catch (e: Exception) {
                _statusText.value = "Key failed: ${e.message?.take(40) ?: "unknown"}"
            } finally {
                _actionInFlight.value = false
            }
        }
    }

    // ── Scroll action — POST /browser/scroll ──
    fun scroll(x: Int, y: Int, direction: String) {
        val api = api ?: return
        _statusText.value = "Scrolled $direction"
        viewModelScope.launch {
            _actionInFlight.value = true
            try {
                api.post(
                    "/browser/scroll",
                    buildJsonObject {
                        put("x", x)
                        put("y", y)
                        put("direction", direction)
                        put("clicks", 3)
                        put("device_id", _selectedDeviceId.value)
                    }.toString()
                )
                delay(350)
                refreshScreenshot()
            } catch (e: Exception) {
                _statusText.value = "Scroll failed: ${e.message?.take(40) ?: "unknown"}"
            } finally {
                _actionInFlight.value = false
            }
        }
    }

    // ── Fetch screenshot: call endpoint for URL, then fetch the actual JPEG bytes ──
    fun refreshScreenshot() {
        val api = api ?: return
        if (fetchingScreenshot) return
        fetchingScreenshot = true
        viewModelScope.launch {
            try {
                val deviceId = _selectedDeviceId.value
                // Step 1: GET /browser/screenshot/live → JSON {"url": "/ui/uploads/xxx.jpg"}
                val json = Json { ignoreUnknownKeys = true }
                val response = api.get("/browser/screenshot/live?device_id=$deviceId")
                val obj = json.parseToJsonElement(response).jsonObject

                // Check for error from backend (e.g., VNC connection failed)
                val error = obj["error"]?.jsonPrimitive?.content
                if (error != null) {
                    _statusText.value = "⚠ $deviceId: $error"
                    return@launch
                }

                val imageUrl = obj["url"]?.jsonPrimitive?.content ?: return@launch

                // Step 2: Fetch actual JPEG bytes from the image URL
                val bytes = api.getBytes(imageUrl)
                if (bytes != null && bytes.size > 100) {
                    _screenshotBytes.value = bytes
                    _statusText.value = "Live — $deviceId"
                } else {
                    _statusText.value = "⚠ Empty screenshot from $deviceId"
                }
            } catch (e: Exception) {
                val msg = e.message?.take(50) ?: "unknown error"
                _statusText.value = "⚠ $msg"
            } finally {
                fetchingScreenshot = false
            }
        }
    }
}

// =============================================================================
// CuScreen Composable — full-featured CU remote desktop viewer
// =============================================================================

@Composable
fun CuScreen(
    origin: String,
    model: String = "",
    cuStep: Int = 0,
    cuStepTotal: Int = 0,
    cuStatus: String = "idle",
    cuActionLabel: String = "",
    // Live CU catalog from ChatViewModel (GET /models/computer-use hydration).
    // liveModels is only trusted when cuModelBackends is non-empty — the
    // backends map is exclusively populated by a successful CU fetch, so a
    // stale chat-provider list can never leak into the CU dropdown. Empty
    // (offline) → Constants.MODEL_CONFIG["computer-use"] + id heuristic.
    liveModels: List<Pair<String, String>> = emptyList(),
    cuModelBackends: Map<String, String> = emptyMap(),
    onModelChange: (String) -> Unit = {},
    onDeviceChange: (String) -> Unit = {},
    onStopCu: () -> Unit = {},
    onNewSession: () -> Unit = {},
    messages: List<com.aiblackbox.portal.data.model.UiMessage> = emptyList(),
    onSpeak: (String) -> Unit = {},
    onSpeakWithId: (String, String) -> Unit = { _, _ -> },
    modifier: Modifier = Modifier,
    viewModel: CuViewModel = viewModel()
) {
    val view = LocalView.current
    val context = LocalContext.current

    val screenshotBytes by viewModel.screenshotBytes.collectAsState()
    val isPolling by viewModel.isPolling.collectAsState()
    val statusText by viewModel.statusText.collectAsState()
    val devices by viewModel.devices.collectAsState()
    val selectedDeviceId by viewModel.selectedDeviceId.collectAsState()
    val actionInFlight by viewModel.actionInFlight.collectAsState()
    val selectedProvider by viewModel.selectedProvider.collectAsState()

    var imageSize by remember { mutableStateOf(IntSize.Zero) }
    var deviceDropdownExpanded by remember { mutableStateOf(false) }
    var providerDropdownExpanded by remember { mutableStateOf(false) }
    var modelDropdownExpanded by remember { mutableStateOf(false) }
    var viewerExpanded by remember { mutableStateOf(true) }
    var showTypingInput by remember { mutableStateOf(false) }
    var typingText by remember { mutableStateOf("") }

    // Decode bytes to bitmap (cached via remember — only re-decodes when bytes change)
    val screenshotBitmap = remember(screenshotBytes) {
        screenshotBytes?.let { bytes ->
            try { BitmapFactory.decodeByteArray(bytes, 0, bytes.size)?.asImageBitmap() }
            catch (_: Exception) { null }
        }
    }

    val isAgentActive = cuStatus == "running"

    LaunchedEffect(origin) {
        viewModel.initialize(origin)
        com.aiblackbox.portal.ui.components.setChatBaseUrl(origin)
    }

    // Auto-start polling when CU agent is running, stop when done
    LaunchedEffect(cuStatus) {
        if (cuStatus == "running" && !isPolling) {
            viewModel.startPolling()
        } else if (cuStatus == "complete" || cuStatus == "stopped") {
            viewModel.stopPolling()
            // Final refresh to show the end state
            viewModel.refreshScreenshot()
        }
    }

    Column(
        modifier = modifier
            .fillMaxSize()
            .background(Color.Black)
    ) {
        // ── Header ──
        CuHeader(
            isPolling = isPolling || isAgentActive,
            statusText = if (isAgentActive) cuActionLabel.ifBlank { "Agent running..." } else statusText,
            actionInFlight = actionInFlight || isAgentActive,
            cuStep = cuStep,
            cuStepTotal = cuStepTotal,
            cuStatus = cuStatus,
            onTogglePolling = {
                view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                if (isPolling) viewModel.stopPolling() else viewModel.startPolling()
            },
            onRefresh = {
                view.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
                viewModel.refreshScreenshot()
            },
            onStop = onStopCu,
            onNewSession = onNewSession
        )

        // ── Provider (backend) + Model selector row ──
        // Provider here is a backend selector (Anthropic vs Google) — local-only for device filtering.
        // The actual chat provider stays "computer-use"; the model ID determines which backend runs.
        CuProviderModelRow(
            selectedBackend = selectedProvider,
            model = model,
            liveModels = liveModels,
            cuModelBackends = cuModelBackends,
            providerExpanded = providerDropdownExpanded,
            modelExpanded = modelDropdownExpanded,
            onProviderExpandedChange = { providerDropdownExpanded = it },
            onModelExpandedChange = { modelDropdownExpanded = it },
            onBackendSelected = { backend ->
                view.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
                viewModel.selectProvider(backend)
                // Auto-select first model for new backend
                val firstModel = cuModelsForBackend(backend, liveModels, cuModelBackends).firstOrNull()?.first
                if (firstModel != null) onModelChange(firstModel)
                providerDropdownExpanded = false
            },
            onModelSelected = { m ->
                view.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
                onModelChange(m)
                modelDropdownExpanded = false
            }
        )

        // ── Device selector ──
        CuDeviceSelector(
            devices = devices,
            selectedDeviceId = selectedDeviceId,
            expanded = deviceDropdownExpanded,
            onExpandedChange = { deviceDropdownExpanded = it },
            onDeviceSelected = { deviceId ->
                view.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
                viewModel.selectDevice(deviceId)
                onDeviceChange(deviceId)
                deviceDropdownExpanded = false
            }
        )

        // ── Collapsible Live View pill / Screenshot viewer ──
        if (viewerExpanded) {
            // Expanded: screenshot viewer with fixed 16:9 aspect ratio image
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .weight(1f)
                    .padding(horizontal = 8.dp, vertical = 4.dp)
                    .background(Neutral100, RoundedCornerShape(RadiusMd))
                    .border(1.dp, Neutral200, RoundedCornerShape(RadiusMd))
                    .clip(RoundedCornerShape(RadiusMd)),
                contentAlignment = Alignment.Center
            ) {
                screenshotBitmap?.let { bmp ->
                    // Direct bitmap rendering — no Coil, no URL loading, always reliable
                    // Fixed 16:9 aspect ratio: taps map 1:1 to display coords
                    androidx.compose.foundation.Image(
                        bitmap = bmp,
                        contentDescription = "Live remote desktop screenshot",
                        modifier = Modifier
                            .fillMaxWidth()
                            .aspectRatio(DISPLAY_WIDTH.toFloat() / DISPLAY_HEIGHT.toFloat())
                            .onGloballyPositioned { imageSize = it.size }
                            .pointerInput(imageSize) {
                                detectTapGestures { offset ->
                                    if (imageSize.width > 0 && imageSize.height > 0) {
                                        val x = (offset.x / imageSize.width * DISPLAY_WIDTH).toInt()
                                            .coerceIn(0, DISPLAY_WIDTH)
                                        val y = (offset.y / imageSize.height * DISPLAY_HEIGHT).toInt()
                                            .coerceIn(0, DISPLAY_HEIGHT)
                                        view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                                        viewModel.click(x, y)
                                        showTypingInput = true
                                    }
                                }
                            },
                        contentScale = ContentScale.FillBounds
                    )
                } ?: Column(
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.Center
                ) {
                    Text(
                        "Remote Desktop",
                        style = MaterialTheme.typography.titleMedium,
                        color = Neutral500,
                        fontWeight = FontWeight.SemiBold
                    )
                    Spacer(Modifier.height(4.dp))
                    Text(
                        "Type a prompt below to command the agent",
                        style = MaterialTheme.typography.bodySmall,
                        color = Neutral700
                    )
                }

                // Minimize button (top-right corner)
                Box(
                    modifier = Modifier
                        .align(Alignment.TopEnd)
                        .padding(8.dp)
                        .clip(RoundedCornerShape(RadiusSm))
                        .background(Color.Black.copy(alpha = 0.6f))
                        .clickable {
                            view.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
                            viewerExpanded = false
                        }
                        .padding(horizontal = 10.dp, vertical = 5.dp)
                ) {
                    Text(
                        "\u25BC Minimize",
                        fontSize = 11.sp,
                        fontWeight = FontWeight.Medium,
                        color = BbxWhite
                    )
                }
            }

            // ── Typing input — appears after tapping the screen ──
            if (showTypingInput) {
                CuTypingInput(
                    text = typingText,
                    onTextChange = { typingText = it },
                    onSend = {
                        if (typingText.isNotBlank()) {
                            view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                            viewModel.typeText(typingText)
                            typingText = ""
                        }
                    },
                    onKey = { key ->
                        view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                        viewModel.sendKey(key)
                    },
                    onDismiss = { showTypingInput = false }
                )
            }

            // Quick actions row
            CuQuickActions(
                onSendKey = { key ->
                    view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                    viewModel.sendKey(key)
                },
                onScroll = { direction ->
                    view.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
                    viewModel.scroll(DISPLAY_WIDTH / 2, DISPLAY_HEIGHT / 2, direction)
                }
            )

            // Status bar
            CuStatusBar(statusText = statusText, isPolling = isPolling || isAgentActive)
        } else {
            // Collapsed: compact pill — tap to expand
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 12.dp, vertical = 6.dp)
                    .clip(RoundedCornerShape(20.dp))
                    .background(CuPurpleBg)
                    .border(1.dp, CuPurpleBorder, RoundedCornerShape(20.dp))
                    .clickable {
                        view.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
                        viewerExpanded = true
                    }
                    .padding(horizontal = 14.dp, vertical = 10.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                // Pulsing green dot when live
                val infiniteTransition = rememberInfiniteTransition(label = "pillPulse")
                val pillAlpha by infiniteTransition.animateFloat(
                    initialValue = 1f, targetValue = 0.4f,
                    animationSpec = infiniteRepeatable(
                        animation = tween(1500, easing = LinearEasing),
                        repeatMode = RepeatMode.Reverse
                    ),
                    label = "pillDotPulse"
                )
                if (isPolling || isAgentActive) {
                    Box(
                        modifier = Modifier
                            .size(8.dp)
                            .clip(CircleShape)
                            .background(CuGreen.copy(alpha = pillAlpha))
                    )
                    Spacer(Modifier.width(8.dp))
                }
                Text(
                    "\u25B2 Live View",
                    fontSize = 13.sp,
                    fontWeight = FontWeight.SemiBold,
                    color = CuPurpleDim
                )
                if (cuStepTotal > 0) {
                    Spacer(Modifier.width(8.dp))
                    Text(
                        "Step $cuStep/$cuStepTotal",
                        fontSize = 11.sp,
                        fontFamily = FontFamily.Monospace,
                        color = Neutral500
                    )
                }
                Spacer(Modifier.weight(1f))
                Text(
                    statusText,
                    fontSize = 11.sp,
                    fontFamily = FontFamily.Monospace,
                    color = Neutral700
                )
            }
        }

        // ── Chat messages (visible when viewer is collapsed, scrollable) ──
        if (!viewerExpanded && messages.isNotEmpty()) {
            val listState = rememberLazyListState()

            // Auto-scroll to latest message
            LaunchedEffect(messages.size, messages.lastOrNull()?.content) {
                if (messages.isNotEmpty()) {
                    listState.animateScrollToItem(messages.size - 1)
                }
            }

            LazyColumn(
                state = listState,
                modifier = Modifier
                    .fillMaxWidth()
                    .weight(1f),
                contentPadding = PaddingValues(top = 4.dp, bottom = 160.dp)
            ) {
                items(
                    items = messages,
                    key = { it.id }
                ) { message ->
                    com.aiblackbox.portal.ui.components.ChatBubble(
                        message = message,
                        onSpeak = onSpeak,
                        onSpeakWithId = onSpeakWithId
                    )
                }
            }
        } else {
            // Bottom padding for Composer overlay
            Spacer(Modifier.height(140.dp))
        }
    }
}

// =============================================================================
// Header — mirrors .cu-interact-header
// =============================================================================

@Composable
private fun CuHeader(
    isPolling: Boolean,
    statusText: String,
    actionInFlight: Boolean,
    cuStep: Int = 0,
    cuStepTotal: Int = 0,
    cuStatus: String = "idle",
    onTogglePolling: () -> Unit,
    onRefresh: () -> Unit,
    onStop: () -> Unit = {},
    onNewSession: () -> Unit = {},
) {
    val infiniteTransition = rememberInfiniteTransition(label = "cuPulse")
    val pulseAlpha by infiniteTransition.animateFloat(
        initialValue = 1f,
        targetValue = 0.5f,
        animationSpec = infiniteRepeatable(
            animation = tween(1500, easing = LinearEasing),
            repeatMode = RepeatMode.Reverse
        ),
        label = "dotPulse"
    )

    val dotColor = when (cuStatus) {
        "running" -> CuGreen.copy(alpha = pulseAlpha)
        "complete" -> CuGreen
        "stopped" -> CuOrange
        else -> if (isPolling) CuGreen.copy(alpha = pulseAlpha) else Neutral500
    }

    Row(
        modifier = Modifier
            .fillMaxWidth()
            .glassSurface(
                shape = RoundedCornerShape(0.dp),
                bg = Neutral150
            )
            .padding(horizontal = 12.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        // Status dot
        Box(
            modifier = Modifier
                .size(8.dp)
                .clip(CircleShape)
                .background(dotColor)
        )
        Spacer(Modifier.width(8.dp))

        // Title
        Text(
            "Computer Use",
            style = MaterialTheme.typography.titleSmall,
            color = BbxWhite,
            fontWeight = FontWeight.SemiBold,
            fontSize = 14.sp
        )

        // Step counter (when agent is active)
        if (cuStepTotal > 0) {
            Spacer(Modifier.width(8.dp))
            Text(
                text = "$cuStep/$cuStepTotal",
                fontSize = 11.sp,
                fontFamily = FontFamily.Monospace,
                color = CuPurpleDim
            )
        }

        Spacer(Modifier.weight(1f))

        // Loading spinner
        AnimatedVisibility(
            visible = actionInFlight,
            enter = fadeIn(tween(DurationFast)),
            exit = fadeOut(tween(DurationFast))
        ) {
            CircularProgressIndicator(
                modifier = Modifier.size(16.dp),
                color = CuPurple,
                strokeWidth = 2.dp
            )
        }

        Spacer(Modifier.width(6.dp))

        // E-Stop button (when agent is running)
        if (cuStatus == "running") {
            CuStopButton(onClick = onStop)
            Spacer(Modifier.width(4.dp))
        }

        // New Session button
        CuHeaderButton(text = "New", onClick = onNewSession)
        Spacer(Modifier.width(4.dp))

        // Refresh button
        CuHeaderButton(text = "\u21BB", onClick = onRefresh)
        Spacer(Modifier.width(4.dp))

        // Live view Start/Stop toggle
        val toggleInteraction = remember { MutableInteractionSource() }
        val togglePressed by toggleInteraction.collectIsPressedAsState()
        val toggleScale by animateFloatAsState(
            targetValue = if (togglePressed) 0.93f else 1f,
            animationSpec = tween(DurationFast, easing = EaseStandard),
            label = "toggleScale"
        )
        val toggleBg by animateColorAsState(
            targetValue = if (isPolling) CuRed.copy(alpha = 0.2f) else SolidGreen.copy(alpha = 0.2f),
            animationSpec = tween(DurationBase, easing = EaseStandard),
            label = "toggleBg"
        )
        val toggleBorder by animateColorAsState(
            targetValue = if (isPolling) CuRed.copy(alpha = 0.5f) else SolidGreen.copy(alpha = 0.5f),
            animationSpec = tween(DurationBase, easing = EaseStandard),
            label = "toggleBorder"
        )

        Box(
            modifier = Modifier
                .scale(toggleScale)
                .clip(PillShape)
                .background(toggleBg)
                .border(1.dp, toggleBorder, PillShape)
                .clickable(
                    interactionSource = toggleInteraction,
                    indication = null,
                    onClick = onTogglePolling
                )
                .padding(horizontal = 12.dp, vertical = 5.dp),
            contentAlignment = Alignment.Center
        ) {
            Text(
                text = if (isPolling) "STOP" else "LIVE",
                fontSize = 11.sp,
                fontWeight = FontWeight.Bold,
                letterSpacing = 0.5.sp,
                color = if (isPolling) CuRed else SolidGreen
            )
        }
    }
}

// =============================================================================
// Header Button
// =============================================================================

@Composable
private fun CuHeaderButton(
    text: String,
    onClick: () -> Unit,
) {
    val interaction = remember { MutableInteractionSource() }
    val pressed by interaction.collectIsPressedAsState()
    val scale by animateFloatAsState(
        targetValue = if (pressed) 0.9f else 1f,
        animationSpec = tween(DurationFast, easing = EaseStandard),
        label = "headerBtnScale"
    )

    Box(
        modifier = Modifier
            .scale(scale)
            .clip(RoundedCornerShape(RadiusSm))
            .background(Color.White.copy(alpha = 0.08f))
            .border(1.dp, Color.White.copy(alpha = 0.12f), RoundedCornerShape(RadiusSm))
            .clickable(
                interactionSource = interaction,
                indication = null,
                onClick = onClick
            )
            .padding(horizontal = 10.dp, vertical = 4.dp),
        contentAlignment = Alignment.Center
    ) {
        Text(
            text = text,
            fontSize = 16.sp,
            color = BbxDim
        )
    }
}

// =============================================================================
// Device Selector — mirrors .cu-drawer-device-row
// =============================================================================

@Composable
private fun CuDeviceSelector(
    devices: List<CuDevice>,
    selectedDeviceId: String,
    expanded: Boolean,
    onExpandedChange: (Boolean) -> Unit,
    onDeviceSelected: (String) -> Unit,
) {
    val selectedDevice = devices.find { it.id == selectedDeviceId }
    val displayName = selectedDevice?.name ?: "BlackBox (Local)"

    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        // Label — matches .cu-drawer-device-label
        Text(
            "DEVICE:",
            fontSize = 10.sp,
            fontWeight = FontWeight.SemiBold,
            letterSpacing = 0.5.sp,
            color = CuPurpleDim
        )
        Spacer(Modifier.width(8.dp))

        // Dropdown trigger — matches .cu-drawer-device-select
        Box {
            Box(
                modifier = Modifier
                    .clip(RoundedCornerShape(RadiusSm))
                    .background(CuPurpleBg)
                    .border(1.dp, CuPurpleBorder, RoundedCornerShape(RadiusSm))
                    .clickable { onExpandedChange(true) }
                    .padding(horizontal = 10.dp, vertical = 5.dp)
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    // Status dot
                    Box(
                        modifier = Modifier
                            .size(6.dp)
                            .clip(CircleShape)
                            .background(
                                if (selectedDevice?.status == "online" || selectedDeviceId == "blackbox")
                                    CuGreen else Neutral500
                            )
                    )
                    Spacer(Modifier.width(6.dp))
                    Text(
                        text = displayName,
                        fontSize = 12.sp,
                        color = CuPurpleDim
                    )
                    Spacer(Modifier.width(6.dp))
                    Text(
                        text = "\u25BE",
                        fontSize = 10.sp,
                        color = Neutral500
                    )
                }
            }

            DropdownMenu(
                expanded = expanded,
                onDismissRequest = { onExpandedChange(false) },
                modifier = Modifier.background(Neutral150)
            ) {
                devices.forEach { device ->
                    val isLocal = device.id == "blackbox" || device.protocol == "local"
                    val isOnline = isLocal || device.status == "online"
                    DropdownMenuItem(
                        text = {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Box(
                                    modifier = Modifier
                                        .size(6.dp)
                                        .clip(CircleShape)
                                        .background(if (isOnline) CuGreen else Neutral500)
                                )
                                Spacer(Modifier.width(8.dp))
                                Text(
                                    text = device.name + if (!isOnline) " (offline)" else "",
                                    color = if (isOnline) BbxWhite else Neutral500,
                                    fontSize = 13.sp
                                )
                            }
                        },
                        onClick = {
                            if (isOnline) onDeviceSelected(device.id)
                        },
                        enabled = isOnline
                    )
                }
            }
        }
    }
}

// =============================================================================
// Provider + Model Row — matches Portal provider/model dropdown pattern
// =============================================================================

private data class CuProviderOption(val id: String, val name: String)

private val CU_BACKENDS = listOf(
    CuProviderOption("anthropic", "Anthropic"),
    CuProviderOption("google", "Google"),
    CuProviderOption("openai", "OpenAI"),
)

/**
 * Partition CU models by backend.
 *
 * Hydrated path (CU production pass 2026-06): when [backends] (the id→backend
 * map from GET /models/computer-use, via ChatViewModel.cuModelBackends) is
 * non-empty, partition [liveModels] by it. The map carries "" → the server
 * default's backend, so the Auto entry lands in that backend's group.
 *
 * Offline path: backends map empty → Constants.MODEL_CONFIG["computer-use"]
 * fallback partitioned by id-substring heuristic (Auto "" goes to anthropic,
 * matching the server's anthropic-default).
 */
private fun cuModelsForBackend(
    backend: String,
    liveModels: List<Pair<String, String>> = emptyList(),
    backends: Map<String, String> = emptyMap(),
): List<Pair<String, String>> {
    if (backends.isNotEmpty() && liveModels.isNotEmpty()) {
        return liveModels.filter { (id, _) -> (backends[id] ?: "anthropic") == backend }
    }
    val all = Constants.MODEL_CONFIG["computer-use"] ?: return emptyList()
    return all.filter { (id, _) ->
        when (backend) {
            "google" -> id.startsWith("gemini")
            "openai" -> id.isNotEmpty() && !id.startsWith("gemini") && !id.startsWith("claude")
            else -> id.isEmpty() || id.startsWith("claude") // anthropic = Auto + claude
        }
    }
}

@Composable
private fun CuProviderModelRow(
    selectedBackend: String,
    model: String,
    liveModels: List<Pair<String, String>>,
    cuModelBackends: Map<String, String>,
    providerExpanded: Boolean,
    modelExpanded: Boolean,
    onProviderExpandedChange: (Boolean) -> Unit,
    onModelExpandedChange: (Boolean) -> Unit,
    onBackendSelected: (String) -> Unit,
    onModelSelected: (String) -> Unit,
) {
    val backendName = CU_BACKENDS.find { it.id == selectedBackend }?.name ?: "Anthropic"
    val models = cuModelsForBackend(selectedBackend, liveModels, cuModelBackends)
    val modelName = models.find { it.first == model }?.second ?: models.firstOrNull()?.second ?: "Auto"

    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp)
    ) {
        // Provider dropdown
        Box {
            Row(
                modifier = Modifier
                    .clip(RoundedCornerShape(RadiusSm))
                    .background(CuPurpleBg)
                    .border(1.dp, CuPurpleBorder, RoundedCornerShape(RadiusSm))
                    .clickable { onProviderExpandedChange(true) }
                    .padding(horizontal = 10.dp, vertical = 6.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text("PROVIDER:", fontSize = 9.sp, fontWeight = FontWeight.SemiBold,
                    letterSpacing = 0.5.sp, color = CuPurpleDim)
                Spacer(Modifier.width(6.dp))
                Text(backendName, fontSize = 12.sp, color = BbxWhite, fontWeight = FontWeight.Medium)
                Spacer(Modifier.width(4.dp))
                Text("\u25BE", fontSize = 10.sp, color = Neutral500)
            }
            DropdownMenu(
                expanded = providerExpanded,
                onDismissRequest = { onProviderExpandedChange(false) },
                modifier = Modifier.background(Neutral150)
            ) {
                CU_BACKENDS.forEach { p ->
                    DropdownMenuItem(
                        text = {
                            Text(p.name,
                                color = if (p.id == selectedBackend) CuPurple else BbxWhite,
                                fontWeight = if (p.id == selectedBackend) FontWeight.Bold else FontWeight.Normal,
                                fontSize = 13.sp)
                        },
                        onClick = { onBackendSelected(p.id) }
                    )
                }
            }
        }

        // Model dropdown
        Box {
            Row(
                modifier = Modifier
                    .clip(RoundedCornerShape(RadiusSm))
                    .background(CuPurpleBg)
                    .border(1.dp, CuPurpleBorder, RoundedCornerShape(RadiusSm))
                    .clickable { onModelExpandedChange(true) }
                    .padding(horizontal = 10.dp, vertical = 6.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text("MODEL:", fontSize = 9.sp, fontWeight = FontWeight.SemiBold,
                    letterSpacing = 0.5.sp, color = CuPurpleDim)
                Spacer(Modifier.width(6.dp))
                Text(modelName, fontSize = 12.sp, color = BbxWhite, fontWeight = FontWeight.Medium)
                Spacer(Modifier.width(4.dp))
                Text("\u25BE", fontSize = 10.sp, color = Neutral500)
            }
            DropdownMenu(
                expanded = modelExpanded,
                onDismissRequest = { onModelExpandedChange(false) },
                modifier = Modifier.background(Neutral150)
            ) {
                models.forEach { (id, name) ->
                    DropdownMenuItem(
                        text = {
                            Text(name,
                                color = if (id == model) CuPurple else BbxWhite,
                                fontWeight = if (id == model) FontWeight.Bold else FontWeight.Normal,
                                fontSize = 13.sp)
                        },
                        onClick = { onModelSelected(id) }
                    )
                }
            }
        }
    }
}

// =============================================================================
// E-Stop Button — matches Portal .cu-drawer-estop
// =============================================================================

@Composable
private fun CuStopButton(onClick: () -> Unit) {
    val interaction = remember { MutableInteractionSource() }
    val pressed by interaction.collectIsPressedAsState()
    val scale by animateFloatAsState(
        targetValue = if (pressed) 0.9f else 1f,
        animationSpec = tween(DurationFast, easing = EaseStandard),
        label = "estopScale"
    )

    Box(
        modifier = Modifier
            .scale(scale)
            .clip(RoundedCornerShape(RadiusSm))
            .background(CuRed.copy(alpha = 0.2f))
            .border(1.dp, CuRed.copy(alpha = 0.5f), RoundedCornerShape(RadiusSm))
            .clickable(
                interactionSource = interaction,
                indication = null,
                onClick = onClick
            )
            .padding(horizontal = 10.dp, vertical = 4.dp),
        contentAlignment = Alignment.Center
    ) {
        Text(
            text = "STOP",
            fontSize = 10.sp,
            fontWeight = FontWeight.ExtraBold,
            letterSpacing = 0.5.sp,
            color = CuRed
        )
    }
}

// =============================================================================
// Quick Actions — compact key buttons + scroll (replaces old typing bar)
// =============================================================================

@Composable
private fun CuQuickActions(
    onSendKey: (String) -> Unit,
    onScroll: (String) -> Unit,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .glassSurface(
                shape = RoundedCornerShape(0.dp),
                bg = Neutral150
            )
            .padding(horizontal = 8.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(4.dp)
    ) {
        Text("KEYS:", fontSize = 9.sp, fontWeight = FontWeight.SemiBold,
            letterSpacing = 0.5.sp, color = CuPurpleDim,
            modifier = Modifier.padding(end = 2.dp))

        CuCompactButton(label = "\u21B5", onClick = { onSendKey("Return") })
        CuCompactButton(label = "\u21E5", onClick = { onSendKey("Tab") })
        CuCompactButton(label = "\u232B", onClick = { onSendKey("BackSpace") })
        CuCompactButton(label = "Esc", onClick = { onSendKey("Escape") })

        Spacer(Modifier.weight(1f))

        CuCompactButton(label = "\u2191", onClick = { onScroll("up") })
        CuCompactButton(label = "\u2193", onClick = { onScroll("down") })
    }
}

@Composable
private fun CuCompactButton(
    label: String,
    onClick: () -> Unit,
) {
    val interaction = remember { MutableInteractionSource() }
    val pressed by interaction.collectIsPressedAsState()
    val bgAlpha by animateFloatAsState(
        targetValue = if (pressed) 0.18f else 0.08f,
        animationSpec = tween(DurationFast, easing = EaseStandard),
        label = "compactBtnBg"
    )

    Box(
        modifier = Modifier
            .clip(RoundedCornerShape(RadiusSm))
            .background(Color.White.copy(alpha = bgAlpha))
            .border(1.dp, Color.White.copy(alpha = 0.12f), RoundedCornerShape(RadiusSm))
            .clickable(
                interactionSource = interaction,
                indication = null,
                onClick = onClick
            )
            .padding(horizontal = 8.dp, vertical = 6.dp),
        contentAlignment = Alignment.Center
    ) {
        Text(
            text = label,
            fontSize = 13.sp,
            color = BbxDim
        )
    }
}

// =============================================================================
// Typing Input — appears after tapping the remote desktop, keyboard pops up
// =============================================================================

@Composable
private fun CuTypingInput(
    text: String,
    onTextChange: (String) -> Unit,
    onSend: () -> Unit,
    onKey: (String) -> Unit,
    onDismiss: () -> Unit,
) {
    val focusRequester = remember { FocusRequester() }

    // Auto-focus to pop up the keyboard immediately
    LaunchedEffect(Unit) {
        focusRequester.requestFocus()
    }

    Row(
        modifier = Modifier
            .fillMaxWidth()
            .glassSurface(
                shape = RoundedCornerShape(0.dp),
                bg = Neutral150
            )
            .padding(horizontal = 8.dp, vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(4.dp)
    ) {
        // Text field — auto-focused to bring up keyboard
        OutlinedTextField(
            value = text,
            onValueChange = onTextChange,
            modifier = Modifier
                .weight(1f)
                .focusRequester(focusRequester),
            placeholder = { Text("Type here...", color = Neutral500, fontSize = 14.sp) },
            colors = OutlinedTextFieldDefaults.colors(
                focusedTextColor = BbxWhite,
                unfocusedTextColor = BbxWhite,
                focusedBorderColor = CuGreen,
                unfocusedBorderColor = CuPurpleBorder,
                cursorColor = CuGreen,
                focusedContainerColor = Neutral100,
                unfocusedContainerColor = Neutral100
            ),
            singleLine = true,
            textStyle = MaterialTheme.typography.bodyMedium.copy(fontSize = 15.sp),
            keyboardOptions = KeyboardOptions(
                imeAction = ImeAction.Send,
                keyboardType = KeyboardType.Text
            ),
            keyboardActions = KeyboardActions(onSend = { onSend() }),
            shape = RoundedCornerShape(RadiusMd)
        )

        // Send button
        CuCompactButton(label = "Send") { onSend() }

        // Enter key
        CuCompactButton(label = "\u21B5") { onKey("Return") }

        // Tab key
        CuCompactButton(label = "\u21E5") { onKey("Tab") }

        // Hide keyboard / dismiss
        CuCompactButton(label = "\u2715") { onDismiss() }
    }
}

// =============================================================================
// Status Bar — mirrors .cu-interact-status
// =============================================================================

@Composable
private fun CuStatusBar(
    statusText: String,
    isPolling: Boolean,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .glassSurface(
                shape = RoundedCornerShape(0.dp),
                bg = Neutral150
            )
            .padding(horizontal = 16.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        // Status text — matches .cu-interact-status-text
        Text(
            text = statusText,
            fontSize = 12.sp,
            fontFamily = FontFamily.Monospace,
            color = Neutral700
        )

        Spacer(Modifier.weight(1f))

        // Coordinate space hint — matches .cu-interact-hint
        Text(
            text = "${DISPLAY_WIDTH}x${DISPLAY_HEIGHT}",
            fontSize = 11.sp,
            fontFamily = FontFamily.Monospace,
            color = Neutral500
        )
    }
}
