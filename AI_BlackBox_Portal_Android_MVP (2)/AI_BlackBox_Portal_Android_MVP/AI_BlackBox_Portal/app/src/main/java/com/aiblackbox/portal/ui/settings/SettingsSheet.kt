@file:Suppress("InlinedApi") // HapticFeedbackConstants.CONFIRM (API 30) degrades gracefully on older APIs

package com.aiblackbox.portal.ui.settings

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.view.HapticFeedbackConstants
import androidx.core.content.edit
import androidx.core.graphics.set
import androidx.core.net.toUri
import androidx.compose.foundation.Image
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.TextButton
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aiblackbox.portal.PairingActivity
import com.google.zxing.BarcodeFormat
import com.google.zxing.qrcode.QRCodeWriter
import com.aiblackbox.portal.data.model.ChatProvider
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.Neutral100
import com.aiblackbox.portal.ui.theme.Neutral200
import com.aiblackbox.portal.ui.theme.Neutral300
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.Neutral700
import com.aiblackbox.portal.ui.theme.RadiusLg
import com.aiblackbox.portal.ui.theme.RadiusMd
import com.aiblackbox.portal.ui.theme.SolidGreen
import com.aiblackbox.portal.util.Constants
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

// =============================================================================
// SettingsSheet — aligned with Portal #menuModal
//
// Portal menu structure:
//   .modal-card (black bg, accent border glow)
//     .menu-body (scrollable, gap: 20px)
//       .generation-section (gradient bg, accent border, grid buttons)
//       .running-apps-section (blue header)
//       .voice-preferences-section (green header)
//       .system-controls-section (gray header)
//
// Android adds: Provider/Model/Operator selectors, Streaming toggle, Native UI
// =============================================================================

@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
fun SettingsSheet(
    origin: String,
    modifier: Modifier = Modifier,
    operators: List<String> = listOf("Brandon"),
    onDismiss: () -> Unit,
    onNavigate: (String) -> Unit = {},
    onClearHistory: () -> Unit = {},
    viewModel: SettingsViewModel = viewModel()
) {
    val context = LocalContext.current
    val provider by viewModel.store.provider.collectAsState(initial = "gemini")
    val currentModel by viewModel.store.model.collectAsState(initial = "")
    val operator by viewModel.store.operator.collectAsState(initial = "Brandon")
    val streaming by viewModel.store.streamingEnabled.collectAsState(initial = true)

    val perProviderModel by viewModel.store.getString("model_$provider", "")
        .collectAsState(initial = "")
    val selectedModel = perProviderModel.ifBlank { currentModel }

    LaunchedEffect(origin) {
        viewModel.initialize(origin)
        viewModel.loadVoiceCatalog()  // after init so api is ready → live catalog drives the picker
    }

    ModalBottomSheet(
        onDismissRequest = onDismiss,
        containerColor = Neutral100,
        contentColor = BbxWhite
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 20.dp, vertical = 8.dp)
        ) {
            // Header (matches Portal .modal-head h3)
            Text(
                "System Menu",
                style = MaterialTheme.typography.headlineMedium.copy(fontWeight = FontWeight.Bold),
                color = BbxWhite
            )
            Spacer(Modifier.height(20.dp))

            // Generation — plain CollapsibleSection, matches the visual weight
            // of Tools/Apps/Gmail (no special gradient card). Per Brandon
            // 2026-05-20: keep it consistent with the other section dropdowns.
            CollapsibleSection(
                title = "\uD83C\uDFA8 Generation",
                accent = BbxAccent,
            ) {
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    FullWidthMenuButton("Generate Image") { onNavigate("image_gen"); onDismiss() }
                    FullWidthMenuButton("Generate Video") { onNavigate("video_gen"); onDismiss() }
                    FullWidthMenuButton("\uD83C\uDFB5 Generate Music") { onNavigate("music_gen"); onDismiss() }
                    FullWidthMenuButton("\uD83D\uDD0A Google SSML") { onNavigate("google_ssml"); onDismiss() }
                    FullWidthMenuButton("\uD83C\uDF99\uFE0F Gemini Pro TTS") { onNavigate("gemini_pro_tts"); onDismiss() }
                    FullWidthMenuButton("\uD83C\uDF99\uFE0F Voice Lab") { onNavigate("voice_lab"); onDismiss() }
                }
            }

            Spacer(Modifier.height(20.dp))

            // ══════════════════════════════════════════════════════════════
            // Floating Overlay — matches Portal #androidOverlaySection
            // ══════════════════════════════════════════════════════════════
            CollapsibleSection(
                title = "\uD83D\uDCF1 Floating Overlay",
                accent = Color(0xFF4A9EFF),
            ) {

            Text(
                "Launch a floating bubble that works anywhere on your device. Voice agents, screenshots, and AI from any app.",
                style = MaterialTheme.typography.bodySmall,
                color = Neutral500,
                modifier = Modifier.padding(bottom = 8.dp)
            )

            // MediaProjection permission launcher — requests screen capture, then starts overlay
            val screenCaptureLauncher = androidx.activity.compose.rememberLauncherForActivityResult(
                contract = androidx.activity.result.contract.ActivityResultContracts.StartActivityForResult()
            ) { result ->
                if (result.resultCode == android.app.Activity.RESULT_OK && result.data != null) {
                    try {
                        val overlayIntent = Intent(context, com.aiblackbox.portal.overlay.OverlayService::class.java).apply {
                            putExtra(com.aiblackbox.portal.overlay.OverlayService.EXTRA_SERVER_URL, origin)
                            putExtra(com.aiblackbox.portal.overlay.OverlayService.EXTRA_OPERATOR, operator)
                            putExtra(com.aiblackbox.portal.overlay.OverlayService.EXTRA_RESULT_CODE, result.resultCode)
                            putExtra(com.aiblackbox.portal.overlay.OverlayService.EXTRA_RESULT_DATA, result.data)
                        }
                        context.startForegroundService(overlayIntent)
                        android.widget.Toast.makeText(context, "Overlay started with screen share", android.widget.Toast.LENGTH_SHORT).show()
                    } catch (e: Exception) {
                        android.widget.Toast.makeText(context, "Overlay failed: ${e.message}", android.widget.Toast.LENGTH_SHORT).show()
                    }
                } else {
                    android.widget.Toast.makeText(context, "Screen capture permission denied", android.widget.Toast.LENGTH_SHORT).show()
                }
                onDismiss()
            }

            // Toggle overlay — check if already running
            val isOverlayActive = com.aiblackbox.portal.overlay.OverlayService.isRunning()
            MenuButton(if (isOverlayActive) "Stop Overlay" else "Start Overlay") {
                if (isOverlayActive) {
                    // Stop the running overlay
                    try {
                        val stopIntent = Intent(context, com.aiblackbox.portal.overlay.OverlayService::class.java).apply {
                            action = com.aiblackbox.portal.overlay.OverlayService.ACTION_STOP
                        }
                        context.startService(stopIntent)
                        android.widget.Toast.makeText(context, "Overlay stopped", android.widget.Toast.LENGTH_SHORT).show()
                        onDismiss()
                    } catch (e: Exception) {
                        android.widget.Toast.makeText(context, "Stop failed: ${e.message}", android.widget.Toast.LENGTH_SHORT).show()
                    }
                } else {
                    // Request screen capture permission first, then start overlay in the callback
                    try {
                        val projMgr = context.getSystemService(android.content.Context.MEDIA_PROJECTION_SERVICE) as android.media.projection.MediaProjectionManager
                        screenCaptureLauncher.launch(projMgr.createScreenCaptureIntent())
                    } catch (e: Exception) {
                        android.widget.Toast.makeText(context, "Overlay failed: ${e.message}", android.widget.Toast.LENGTH_SHORT).show()
                    }
                }
            }
            } // end CollapsibleSection (Floating Overlay)

            Spacer(Modifier.height(20.dp))

            // ══════════════════════════════════════════════════════════════
            // Tools Navigation — matches Portal menu items
            // ══════════════════════════════════════════════════════════════
            CollapsibleSection(
                title = "\uD83D\uDE80 Tools",
                accent = Color(0xFF4A9EFF),
            ) {
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {

                    FullWidthMenuButton("Timeline") { onNavigate("timeline"); onDismiss() }
                    FullWidthMenuButton("Media Browser") { onNavigate("media"); onDismiss() }
                    FullWidthMenuButton("Computer Use") { onNavigate("computer_use"); onDismiss() }
                    FullWidthMenuButton("CLI Agents") { onNavigate("cli_agent"); onDismiss() }
                    FullWidthMenuButton("Voice Agent") { onNavigate("voice"); onDismiss() }
                    FullWidthMenuButton("Scheduler") { onNavigate("cron"); onDismiss() }
                    FullWidthMenuButton("Devices") { onNavigate("devices"); onDismiss() }
                    FullWidthMenuButton("Telephony") { onNavigate("telephony"); onDismiss() }
                    FullWidthMenuButton("SMS Inbox") { onNavigate("sms_inbox"); onDismiss() }
                    FullWidthMenuButton("Contacts") { onNavigate("contacts"); onDismiss() }
                    FullWidthMenuButton("Cellular") { onNavigate("cellular"); onDismiss() }
                    FullWidthMenuButton("↻  Updates") { onNavigate("updates"); onDismiss() }
                }
            } // end CollapsibleSection (Tools)

            Spacer(Modifier.height(20.dp))

            // ══════════════════════════════════════════════════════════════
            // Running Apps — matches Portal .running-apps-section
            // Fetches from GET /agent/apps, opens via reverse proxy
            // ══════════════════════════════════════════════════════════════
            val apps by viewModel.apps.collectAsState()

            CollapsibleSection(
                title = "\uD83D\uDE80 Running Apps",
                accent = Color(0xFF4A9EFF),
                subtitle = if (apps.isNotEmpty()) "${apps.size} running" else null,
            ) {

            if (apps.isEmpty()) {
                Text(
                    "No apps running",
                    style = MaterialTheme.typography.bodySmall,
                    color = Neutral500,
                    modifier = Modifier.padding(vertical = 8.dp)
                )
            } else {
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    apps.forEach { app ->
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
                                .clip(RoundedCornerShape(RadiusMd))
                                .background(Neutral200)
                                .border(1.dp, GlassBorder, RoundedCornerShape(RadiusMd))
                                .clickable {
                                    // Open app via reverse proxy in browser
                                    val proxyUrl = "$origin/app-proxy/${app.port}/"
                                    try {
                                        context.startActivity(
                                            Intent(Intent.ACTION_VIEW, proxyUrl.toUri())
                                        )
                                    } catch (_: Exception) {}
                                }
                                .padding(12.dp),
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Column(Modifier.weight(1f)) {
                                Text(
                                    app.name,
                                    style = MaterialTheme.typography.bodySmall.copy(fontWeight = FontWeight.Medium),
                                    color = BbxWhite
                                )
                                Text(
                                    ":${app.port}",
                                    style = MaterialTheme.typography.labelSmall.copy(fontFamily = FontFamily.Monospace),
                                    color = Neutral500
                                )
                            }
                            Text(
                                "Open \u2192",
                                style = MaterialTheme.typography.labelSmall,
                                color = Color(0xFF4A9EFF)
                            )
                        }
                    }
                }
            }
            } // end CollapsibleSection (Running Apps)

            Spacer(Modifier.height(20.dp))

            // ══════════════════════════════════════════════════════════════
            // Gmail — per-operator OAuth connection
            // ══════════════════════════════════════════════════════════════
            var gmailStatus by remember { mutableStateOf<String?>(null) }
            var gmailEmail by remember { mutableStateOf("") }
            var gmailLoading by remember { mutableStateOf(true) }
            var gmailCheckTrigger by remember { mutableIntStateOf(0) }

            // Check Gmail connection status — re-checks when trigger increments
            LaunchedEffect(operator, gmailCheckTrigger) {
                gmailLoading = true
                try {
                    val response = viewModel.api?.get("/gmail/status/$operator")
                    if (response != null) {
                        val obj = kotlinx.serialization.json.Json.parseToJsonElement(response).jsonObject
                        val connected = obj["connected"]?.jsonPrimitive?.content == "true"
                        gmailEmail = obj["email"]?.jsonPrimitive?.content ?: ""
                        gmailStatus = if (connected) "connected" else "disconnected"
                    }
                } catch (_: Exception) {
                    gmailStatus = "disconnected"
                }
                gmailLoading = false
            }

            CollapsibleSection(
                title = "📧 Gmail",
                accent = Color(0xFF4A9EFF),
                subtitle = gmailEmail.takeIf { gmailStatus == "connected" && it.isNotBlank() },
            ) {
            if (gmailLoading) {
                Text("Checking Gmail...", style = MaterialTheme.typography.bodySmall, color = Neutral500)
            } else if (gmailStatus == "connected") {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Box(
                        Modifier.size(8.dp).clip(RoundedCornerShape(50))
                            .background(SolidGreen)
                    )
                    Spacer(Modifier.width(8.dp))
                    Text(
                        "Connected: $gmailEmail",
                        style = MaterialTheme.typography.bodySmall.copy(fontWeight = FontWeight.Medium),
                        color = SolidGreen
                    )
                }
                Spacer(Modifier.height(8.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    MenuButton("\uD83D\uDD17 Disconnect") {
                        viewModel.disconnectGmail(operator)
                        gmailStatus = "disconnected"
                        gmailEmail = ""
                    }
                    MenuButton("\uD83D\uDD04 Reconnect") {
                        try {
                            val authUrl = "$origin/auth/gmail/authorize?operator=$operator"
                            context.startActivity(
                                Intent(Intent.ACTION_VIEW, authUrl.toUri())
                            )
                        } catch (_: Exception) {}
                    }
                }
            } else {
                Text(
                    "Connect your Gmail to let AI read, search, and send emails.",
                    style = MaterialTheme.typography.bodySmall,
                    color = Neutral500,
                    modifier = Modifier.padding(bottom = 8.dp)
                )
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    MenuButton("\uD83D\uDCE7 Connect Gmail") {
                        try {
                            val authUrl = "$origin/auth/gmail/authorize?operator=$operator"
                            context.startActivity(
                                Intent(Intent.ACTION_VIEW, authUrl.toUri())
                            )
                        } catch (_: Exception) {}
                    }
                    MenuButton("\uD83D\uDD04 Refresh") {
                        gmailCheckTrigger++
                    }
                }
            }
            } // end CollapsibleSection (Gmail)

            Spacer(Modifier.height(20.dp))

            // ══════════════════════════════════════════════════════════════
            // Voice Preferences — single dropdown (all voices grouped)
            // ══════════════════════════════════════════════════════════════
            SectionHeader("\uD83D\uDD0A Voice", SolidGreen)

            val currentVoice by viewModel.store.getOperatorVoice(operator)
                .collectAsState(initial = "openai:onyx")
            var showVoiceMenu by remember { mutableStateOf(false) }

            // Find current voice display name — render the FETCHED catalog so the
            // picker always reflects the backend (falls back to offline default).
            // Load is chained after initialize(origin) (line ~130) so api is ready.
            val allVoiceGroups by viewModel.voiceGroups.collectAsState()
            val currentVoiceDisplay = allVoiceGroups.flatMap { it.voices }
                .find { it.id == currentVoice }?.let { "${it.name} — ${it.description}" } ?: currentVoice

            // Single ▶ preview button beside the dropdown — synthesizes a fixed
            // phrase for the CURRENTLY selected voice and plays it (mirrors the
            // web portal's #btnPreviewVoice). Loading state + overlap guard live
            // in the VM (previewing). Errors surface via Toast.
            val previewing by viewModel.previewing.collectAsState()
            val previewError by viewModel.previewError.collectAsState()
            LaunchedEffect(previewError) {
                previewError?.let {
                    android.widget.Toast.makeText(context, it, android.widget.Toast.LENGTH_SHORT).show()
                    viewModel.clearPreviewError()
                }
            }

            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.weight(1f)) {
            SettingsDropdown(
                label = "TTS Voice",
                value = currentVoiceDisplay,
                expanded = showVoiceMenu,
                onExpandedChange = { showVoiceMenu = it },
                accentColor = SolidGreen
            ) {
                allVoiceGroups.forEach { group ->
                    // Group header
                    DropdownMenuItem(
                        text = {
                            Text(
                                group.label,
                                color = if (group.label.contains("Gemini")) SolidGreen else BbxDim,
                                fontWeight = FontWeight.Bold,
                                fontSize = 12.sp
                            )
                        },
                        onClick = {},
                        enabled = false
                    )
                    group.voices.forEach { voice ->
                        val isSelected = currentVoice == voice.id
                        DropdownMenuItem(
                            text = {
                                Text(
                                    "${voice.name} — ${voice.description}",
                                    color = if (isSelected) SolidGreen else BbxWhite,
                                    fontWeight = if (isSelected) FontWeight.Bold else FontWeight.Normal,
                                    fontSize = 13.sp
                                )
                            },
                            onClick = {
                                viewModel.setOperatorVoice(operator, voice.id)
                                showVoiceMenu = false
                            }
                        )
                    }
                }
            }
                } // end Box(weight=1f) wrapping the voice dropdown
                IconButton(
                    enabled = !previewing,
                    onClick = { viewModel.previewVoice(currentVoice) }
                ) {
                    Text(if (previewing) "…" else "▶", color = SolidGreen)
                }
                // Voice Lab affordance — put it where web users (and Brandon) expect
                // it: beside the voice picker. The screen itself gates on ElevenLabs
                // status, so always-show is safe. (Menu item at "Generation" stays too.)
                IconButton(
                    onClick = { onNavigate("voice_lab"); onDismiss() }
                ) {
                    Text("🎙️", color = SolidGreen)
                }
            } // end Row (voice dropdown + ▶ preview + Voice Lab)

            Spacer(Modifier.height(16.dp))

            // ══════════════════════════════════════════════════════════════
            // Provider — dropdown
            // ══════════════════════════════════════════════════════════════
            SectionHeader("Provider", BbxDim)

            var showProviderMenu by remember { mutableStateOf(false) }
            val providerDisplay = ChatProvider.fromId(provider).displayName

            SettingsDropdown(
                label = "Provider",
                value = providerDisplay,
                expanded = showProviderMenu,
                onExpandedChange = { showProviderMenu = it }
            ) {
                ChatProvider.entries.forEach { p ->
                    DropdownMenuItem(
                        text = {
                            Text(
                                p.displayName,
                                color = if (p.id == provider) BbxAccent else BbxWhite,
                                fontWeight = if (p.id == provider) FontWeight.Bold else FontWeight.Normal
                            )
                        },
                        onClick = {
                            viewModel.setProvider(p.id)
                            showProviderMenu = false
                            // Auto-navigate for providers with dedicated screens
                            val targetRoute = when (p.id) {
                                "computer-use" -> "computer_use"
                                "gemini-live", "grok-live", "realtime" -> "voice"
                                "agents" -> "agent"
                                "gemini-agents" -> "gemini_agent"
                                else -> null
                            }
                            if (targetRoute != null) {
                                onNavigate(targetRoute)
                                onDismiss()
                            }
                        }
                    )
                }
            }

            Spacer(Modifier.height(16.dp))

            // ══════════════════════════════════════════════════════════════
            // Model — dropdown (dynamic per provider)
            // ══════════════════════════════════════════════════════════════
            val models = Constants.MODEL_CONFIG[provider]
            if (models != null && models.isNotEmpty()) {
                var showModelMenu by remember { mutableStateOf(false) }
                val modelDisplay = models.find { it.first == selectedModel }?.second ?: "Auto"

                SectionHeader("Model", BbxDim)

                SettingsDropdown(
                    label = "Model",
                    value = modelDisplay,
                    expanded = showModelMenu,
                    onExpandedChange = { showModelMenu = it }
                ) {
                    models.forEach { (modelId, displayName) ->
                        DropdownMenuItem(
                            text = {
                                Text(
                                    displayName,
                                    color = if (selectedModel == modelId) BbxAccent else BbxWhite,
                                    fontWeight = if (selectedModel == modelId) FontWeight.Bold else FontWeight.Normal
                                )
                            },
                            onClick = {
                                viewModel.setModel(modelId, provider)
                                showModelMenu = false
                            }
                        )
                    }
                }

                Spacer(Modifier.height(16.dp))
            }

            // ══════════════════════════════════════════════════════════════
            // On-Device Model (Gemma) — Model Manager (Task 1.5)
            // Built lazily once the api + operator are ready; disposed on leave.
            // onModelSelected records the chosen slug under "model_local" so the
            // picker (Task 1.6) can read the active on-device model.
            // ══════════════════════════════════════════════════════════════
            val localApi = viewModel.api
            if (localApi != null) {
                val localModelVm = remember(localApi, operator) {
                    LocalModelViewModel.fromContext(
                        context = context,
                        api = localApi,
                        operatorProvider = { operator },
                        onModelSelected = { slug -> viewModel.setModel(slug, "local") },
                    )
                }
                DisposableEffect(localModelVm) {
                    localModelVm.refresh()
                    onDispose { localModelVm.dispose() }
                }
                LocalModelSection(viewModel = localModelVm)
                Spacer(Modifier.height(20.dp))
            }

            // ══════════════════════════════════════════════════════════════
            // Operator — dropdown + add new
            // ══════════════════════════════════════════════════════════════
            SectionHeader("Operator", BbxDim)

            var showOperatorMenu by remember { mutableStateOf(false) }
            var showAddOperatorDialog by remember { mutableStateOf(false) }
            var newOperatorName by remember { mutableStateOf("") }

            SettingsDropdown(
                label = "Operator",
                value = operator,
                expanded = showOperatorMenu,
                onExpandedChange = { showOperatorMenu = it }
            ) {
                operators.forEach { op ->
                    DropdownMenuItem(
                        text = {
                            Text(
                                op,
                                color = if (op == operator) BbxAccent else BbxWhite,
                                fontWeight = if (op == operator) FontWeight.Bold else FontWeight.Normal
                            )
                        },
                        onClick = {
                            viewModel.setOperator(op)
                            showOperatorMenu = false
                        }
                    )
                }
                // Divider + Add new
                Box(Modifier.fillMaxWidth().padding(horizontal = 12.dp).height(1.dp).background(GlassBorder))
                DropdownMenuItem(
                    text = { Text("+ New Operator", color = BbxAccent, fontWeight = FontWeight.Medium) },
                    onClick = {
                        showOperatorMenu = false
                        newOperatorName = ""
                        showAddOperatorDialog = true
                    }
                )
            }

            // Add operator dialog
            if (showAddOperatorDialog) {
                AlertDialog(
                    onDismissRequest = { showAddOperatorDialog = false },
                    title = { Text("New Operator", color = BbxWhite) },
                    text = {
                        androidx.compose.material3.OutlinedTextField(
                            value = newOperatorName,
                            onValueChange = { newOperatorName = it },
                            placeholder = { Text("Operator name", color = Neutral500) },
                            singleLine = true,
                            colors = androidx.compose.material3.OutlinedTextFieldDefaults.colors(
                                focusedTextColor = BbxWhite,
                                unfocusedTextColor = BbxWhite,
                                focusedBorderColor = BbxAccent,
                                unfocusedBorderColor = Neutral300,
                                cursorColor = BbxAccent
                            )
                        )
                    },
                    confirmButton = {
                        TextButton(onClick = {
                            val name = newOperatorName.trim()
                            if (name.isNotBlank()) {
                                viewModel.addOperator(name)
                                showAddOperatorDialog = false
                            }
                        }) { Text("Create", color = BbxAccent) }
                    },
                    dismissButton = {
                        TextButton(onClick = { showAddOperatorDialog = false }) {
                            Text("Cancel", color = Neutral500)
                        }
                    },
                    containerColor = Neutral100
                )
            }

            Spacer(Modifier.height(16.dp))

            // ══════════════════════════════════════════════════════════════
            // Streaming toggle
            // ══════════════════════════════════════════════════════════════
            ToggleRow(
                label = "Streaming",
                checked = streaming,
                onCheckedChange = { viewModel.setStreamingEnabled(it) },
                checkedColor = BbxAccent
            )

            Spacer(Modifier.height(8.dp))

            // ══════════════════════════════════════════════════════════════
            // Native UI toggle
            // ══════════════════════════════════════════════════════════════
            ToggleRow(
                label = "Native UI (uncheck for WebView)",
                checked = true,
                onCheckedChange = { viewModel.setUseNative(context, it) },
                checkedColor = SolidGreen
            )

            Spacer(Modifier.height(20.dp))

            // ══════════════════════════════════════════════════════════════
            // System Controls
            // ══════════════════════════════════════════════════════════════
            SectionHeader("\u2699\uFE0F System", Neutral700)

            val view = LocalView.current
            var showRepairConfirm by remember { mutableStateOf(false) }

            // Paired device info
            Box(
                Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(RadiusMd))
                    .background(Neutral200)
                    .padding(12.dp)
            ) {
                Column {
                    Text("Paired Server", style = MaterialTheme.typography.labelSmall, color = Neutral500)
                    Spacer(Modifier.height(4.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(
                            origin.ifBlank { "Not paired" },
                            style = MaterialTheme.typography.bodySmall.copy(fontFamily = FontFamily.Monospace),
                            color = if (origin.isNotBlank()) SolidGreen else BbxAccent,
                            modifier = Modifier.weight(1f),
                        )
                        if (origin.isNotBlank()) {
                            IconButton(
                                onClick = {
                                    val clip = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                                    clip.setPrimaryClip(ClipData.newPlainText("Paired Server", origin))
                                    view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                                    android.widget.Toast.makeText(context, "Copied $origin", android.widget.Toast.LENGTH_SHORT).show()
                                },
                                modifier = Modifier.size(32.dp),
                            ) {
                                // Emoji clipboard fallback: material-icons-core (this project's only icons set)
                                // doesn't include filled.ContentCopy; emoji renders identically across devices.
                                Text(
                                    text = "📋",
                                    style = MaterialTheme.typography.bodyMedium,
                                )
                            }
                        }
                    }
                }
            }

            Spacer(Modifier.height(12.dp))

            // Action buttons grid
            FlowRow(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                // Checkpoint
                MenuButton("\uD83D\uDCBE Checkpoint") {
                    view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                    viewModel.triggerCheckpoint()
                }

                // Clear History
                MenuButton("\uD83D\uDDD1 Clear History") {
                    view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                    onClearHistory(); onDismiss()
                }

                // Cancel Stuck Tasks
                MenuButton("\u26D4 Cancel Tasks") {
                    view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                    viewModel.cancelAllTasks()
                }

                // Re-pair Device
                MenuButton("\uD83D\uDD17 Re-pair Device") {
                    view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                    showRepairConfirm = true
                }

                // Restart Service
                MenuButton("\uD83D\uDD04 Restart Service") {
                    view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                    viewModel.restartService()
                }

                // Pair New Device — show QR code for another device to scan
                var showPairQr by remember { mutableStateOf(false) }
                MenuButton("\uD83D\uDCF2 Pair New Device") {
                    view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                    showPairQr = true
                }

                if (showPairQr) {
                    PairNewDeviceDialog(
                        origin = origin,
                        operator = operator,
                        onDismiss = { showPairQr = false }
                    )
                }

                // Manage Setup — opens the onboarding wizard in manage mode for credential edits.
                // Single source of truth: same UI used at first-run setup, no Android-side reimplementation.
                MenuButton("⚙️ Manage Setup") {
                    view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                    val target = if (origin.isNotBlank()) "$origin/onboarding/?mode=manage"
                                 else "http://localhost:9091/onboarding/?mode=manage"
                    try {
                        val intent = Intent(Intent.ACTION_VIEW, target.toUri())
                        intent.flags = Intent.FLAG_ACTIVITY_NEW_TASK
                        context.startActivity(intent)
                    } catch (_: Exception) {}
                    onDismiss()
                }
            }

            // Re-pair confirmation dialog
            if (showRepairConfirm) {
                AlertDialog(
                    onDismissRequest = { showRepairConfirm = false },
                    title = { Text("Re-pair Device", color = BbxWhite) },
                    text = {
                        Text(
                            "This will disconnect from the current server and open the QR pairing screen. Continue?",
                            color = BbxDim
                        )
                    },
                    confirmButton = {
                        TextButton(onClick = {
                            view.performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                            showRepairConfirm = false
                            // Clear origin from SharedPreferences and launch PairingActivity
                            context.getSharedPreferences(Constants.PREFS_NAME, android.content.Context.MODE_PRIVATE)
                                .edit { remove(Constants.KEY_ORIGIN) }
                            context.startActivity(
                                Intent(context, PairingActivity::class.java)
                                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)
                            )
                        }) {
                            Text("Re-pair", color = BbxAccent)
                        }
                    },
                    dismissButton = {
                        TextButton(onClick = { showRepairConfirm = false }) {
                            Text("Cancel", color = Neutral500)
                        }
                    },
                    containerColor = Neutral100
                )
            }

            Spacer(Modifier.height(24.dp))

            // Version info
            Text(
                "AI BlackBox Portal \u00B7 Native v1.0",
                style = MaterialTheme.typography.labelSmall,
                color = Neutral500
            )
            Spacer(Modifier.height(16.dp))
        }
    }
}

// =============================================================================
// Reusable components
// =============================================================================

/** Section header with colored label — matches Portal .menu-group h4 */
@Composable
private fun SectionHeader(text: String, color: Color = BbxAccent) {
    Column(modifier = Modifier.padding(bottom = 8.dp)) {
        Text(
            text,
            style = MaterialTheme.typography.labelLarge.copy(
                fontWeight = FontWeight.SemiBold,
                fontSize = 14.sp
            ),
            color = color
        )
        Spacer(Modifier.height(6.dp))
        Box(
            Modifier
                .fillMaxWidth()
                .height(1.dp)
                .background(Neutral300)
        )
    }
}

/** Menu grid button — matches Portal .btn.btn-generation */
@Composable
private fun MenuButton(label: String, modifier: Modifier = Modifier, onClick: () -> Unit) {
    Box(
        modifier = modifier
            .clip(RoundedCornerShape(RadiusMd))
            .background(Neutral200)
            .border(1.dp, GlassBorder, RoundedCornerShape(RadiusMd))
            .clickable(onClick = onClick)
            .padding(horizontal = 14.dp, vertical = 10.dp)
    ) {
        Text(
            label,
            style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Medium),
            color = BbxWhite
        )
    }
}

/** Full-width MenuButton — used inside CollapsibleSection vertical Columns
 *  so each item stretches across the dropdown content area (one-per-line UX). */
@Composable
private fun FullWidthMenuButton(label: String, onClick: () -> Unit) {
    MenuButton(label = label, modifier = Modifier.fillMaxWidth(), onClick = onClick)
}

/** Collapsible section with tappable header (chevron rotates 180° on expand).
 *  rememberSaveable so expand/collapse survives rotation; no cross-session persistence. */
@Composable
private fun CollapsibleSection(
    title: String,
    accent: Color = BbxAccent,
    subtitle: String? = null,
    initiallyExpanded: Boolean = false,
    content: @Composable () -> Unit,
) {
    var expanded by rememberSaveable { mutableStateOf(initiallyExpanded) }
    Column(modifier = Modifier.fillMaxWidth()) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .clip(RoundedCornerShape(RadiusMd))
                .clickable { expanded = !expanded }
                .padding(vertical = 8.dp, horizontal = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = title,
                style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.SemiBold),
                color = accent,
                modifier = Modifier.weight(1f),
            )
            subtitle?.let {
                Text(
                    text = it,
                    style = MaterialTheme.typography.bodySmall,
                    color = Neutral500,
                    modifier = Modifier.padding(end = 8.dp),
                )
            }
            val rotation by animateFloatAsState(
                targetValue = if (expanded) 180f else 0f,
                label = "chevron",
            )
            Icon(
                imageVector = Icons.Default.KeyboardArrowDown,
                contentDescription = if (expanded) "Collapse" else "Expand",
                modifier = Modifier.rotate(rotation),
                tint = accent,
            )
        }
        AnimatedVisibility(visible = expanded) {
            Column(modifier = Modifier.padding(top = 4.dp, bottom = 8.dp)) {
                content()
            }
        }
    }
}

/** Settings dropdown — glass-styled dropdown for Voice, Provider, Model, Operator */
@Composable
private fun SettingsDropdown(
    @Suppress("unused") label: String,
    value: String,
    expanded: Boolean,
    onExpandedChange: (Boolean) -> Unit,
    @Suppress("unused") accentColor: Color = BbxAccent,
    content: @Composable androidx.compose.foundation.layout.ColumnScope.() -> Unit
) {
    Box {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .clip(RoundedCornerShape(RadiusMd))
                .background(Neutral200)
                .border(1.dp, GlassBorder, RoundedCornerShape(RadiusMd))
                .clickable { onExpandedChange(true) }
                .padding(horizontal = 14.dp, vertical = 12.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text(
                value,
                style = MaterialTheme.typography.bodyMedium.copy(fontWeight = FontWeight.Medium),
                color = BbxWhite,
                modifier = Modifier.weight(1f),
                maxLines = 1
            )
            Text("\u25BE", color = Neutral500)
        }
        DropdownMenu(
            expanded = expanded,
            onDismissRequest = { onExpandedChange(false) },
            modifier = Modifier
                .background(Neutral100)
                .heightIn(max = 400.dp)
        ) {
            content()
        }
    }
}

// =============================================================================
// Pair New Device Dialog — generates QR code for another device to scan
// =============================================================================

@Composable
private fun PairNewDeviceDialog(
    origin: String,
    operator: String,
    onDismiss: () -> Unit,
    viewModel: SettingsViewModel = viewModel()
) {
    // Fetch pairing token from backend on open (matches Portal POST /pair/start flow)
    var pairToken by remember { mutableStateOf<String?>(null) }
    var pairExp by remember { mutableLongStateOf(0L) }
    var isLoading by remember { mutableStateOf(true) }

    LaunchedEffect(Unit) {
        val tokenData = viewModel.fetchPairToken()
        pairToken = tokenData?.first
        pairExp = tokenData?.second ?: 0L
        isLoading = false
    }

    // Build the full pairing payload (matches Portal ui-setup.js lines 811-812)
    val qrPayload = remember(pairToken) {
        if (pairToken != null) {
            """{"type":"pair","token":"$pairToken","exp":$pairExp,"origin":"$origin","operator":"$operator"}"""
        } else {
            // Fallback: simple payload without token (still works for scanning)
            """{"origin":"$origin","operator":"$operator"}"""
        }
    }
    val qrBitmap = remember(qrPayload) { generateQrBitmap(qrPayload, 512) }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = {
            Text(
                "\uD83D\uDCF2 Pair New Device",
                color = BbxWhite,
                style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.Bold)
            )
        },
        text = {
            Column(
                horizontalAlignment = Alignment.CenterHorizontally,
                modifier = Modifier.fillMaxWidth()
            ) {
                Text(
                    "Scan this QR code from another device running AI BlackBox Portal to connect it to the same server.",
                    style = MaterialTheme.typography.bodySmall,
                    color = BbxDim,
                    textAlign = TextAlign.Center
                )
                Spacer(Modifier.height(16.dp))

                if (isLoading) {
                    Text("Generating pairing code...", color = Neutral500, style = MaterialTheme.typography.bodySmall)
                } else if (qrBitmap != null) {
                    Box(
                        Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(RadiusMd))
                            .background(BbxWhite)
                            .padding(16.dp),
                        contentAlignment = Alignment.Center
                    ) {
                        Image(
                            bitmap = qrBitmap.asImageBitmap(),
                            contentDescription = "Pairing QR Code",
                            modifier = Modifier.size(240.dp)
                        )
                    }
                } else {
                    Text("Failed to generate QR", color = BbxAccent)
                }

                Spacer(Modifier.height(12.dp))

                Text(
                    origin,
                    style = MaterialTheme.typography.labelSmall.copy(fontFamily = FontFamily.Monospace),
                    color = Neutral500,
                    textAlign = TextAlign.Center
                )

                if (pairToken != null) {
                    Text(
                        "Expires in 5 minutes",
                        style = MaterialTheme.typography.labelSmall,
                        color = Neutral500
                    )
                }
            }
        },
        confirmButton = {
            TextButton(onClick = onDismiss) {
                Text("Done", color = BbxAccent)
            }
        },
        containerColor = Neutral100
    )
}

/** Generate a QR code bitmap using ZXing (already in project deps) */
private fun generateQrBitmap(content: String, size: Int = 512): Bitmap? {
    return try {
        val writer = QRCodeWriter()
        val bitMatrix = writer.encode(content, BarcodeFormat.QR_CODE, size, size)
        val bitmap = Bitmap.createBitmap(size, size, Bitmap.Config.RGB_565)
        for (x in 0 until size) {
            for (y in 0 until size) {
                bitmap[x, y] = if (bitMatrix.get(x, y)) android.graphics.Color.BLACK else android.graphics.Color.WHITE
            }
        }
        bitmap
    } catch (_: Exception) {
        null
    }
}

/** Toggle row — used for Streaming, Native UI */
@Composable
private fun ToggleRow(
    label: String,
    checked: Boolean,
    onCheckedChange: (Boolean) -> Unit,
    checkedColor: Color = BbxAccent
) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically
    ) {
        Text(label, style = MaterialTheme.typography.bodyMedium, color = BbxDim)
        Switch(
            checked = checked,
            onCheckedChange = onCheckedChange,
            colors = SwitchDefaults.colors(
                checkedThumbColor = checkedColor,
                checkedTrackColor = checkedColor.copy(alpha = 0.3f),
                uncheckedThumbColor = Neutral500,
                uncheckedTrackColor = Neutral300
            )
        )
    }
}
