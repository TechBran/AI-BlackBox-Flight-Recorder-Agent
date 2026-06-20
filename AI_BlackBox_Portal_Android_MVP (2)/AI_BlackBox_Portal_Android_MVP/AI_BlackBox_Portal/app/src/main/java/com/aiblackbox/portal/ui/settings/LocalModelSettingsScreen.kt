package com.aiblackbox.portal.ui.settings

import android.app.ActivityManager
import android.content.Context
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.foundation.text.KeyboardOptions
import com.aiblackbox.portal.data.local.LiteRtEngine
import com.aiblackbox.portal.ui.chat.ChatViewModel
import com.aiblackbox.portal.ui.chat.LocalEngineState
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxBlack
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.Neutral200
import com.aiblackbox.portal.ui.theme.Neutral300
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.RadiusMd
import com.aiblackbox.portal.ui.theme.RadiusSm
import com.aiblackbox.portal.ui.theme.SolidGreen

/**
 * On-Device Model SETTINGS screen — tune the lean phone Gemma tool-caller on the
 * device itself. A full nav destination (route `local_model_settings`, opened from
 * the [SettingsSheet] "On-Device Model" section) so it has room for the slider +
 * sampler + status readout that don't fit the picker section.
 *
 * Wires ONLY to existing headless seams on [ChatViewModel] (built in prior tasks):
 *  - context-window slider  -> [ChatViewModel.applyLocalModelSettings] (maxTokens),
 *    which persists the per-model config AND re-warms the engine;
 *  - sampler (temp/topK/topP) -> [ChatViewModel.applyLocalModelSettings] (sampler);
 *  - auto-warm Switch       -> [ChatViewModel.autoWarmEnabled]/[setAutoWarmEnabled]
 *    (the [com.aiblackbox.portal.data.local.LocalWarmPrefs] store);
 *  - clear conversation     -> [ChatViewModel.clearLocalConversation] (confirm-gated);
 *  - status readout         -> [ChatViewModel.localEngineState] (loaded?) + the active
 *    [com.aiblackbox.portal.data.local.ModelConfig]'s window + device free RAM.
 *
 * All ranges / the recommended marker / sampler defaults come from [LiteRtEngine]
 * constants — never hardcoded. All decision/format logic is the pure, unit-tested
 * mappers in LocalModelSettingsUiState.kt ([windowWarning], [engineStatusLabel],
 * [formatFreeRam], [clampWindow]); this Composable just applies them.
 */
@Composable
fun LocalModelSettingsScreen(
    modifier: Modifier = Modifier,
    viewModel: ChatViewModel,
) {
    val context = LocalContext.current
    val engineState by viewModel.localEngineState.collectAsState()

    // Pre-fill from the ACTIVE model's persisted config (seam:
    // currentLocalModelConfig); fall back to the engine DEFAULT_* constants for any
    // unset axis. Loaded once when the screen opens.
    var windowValue by remember { mutableStateOf(LiteRtEngine.DEFAULT_MAX_TOKENS) }
    var appliedWindow by remember { mutableStateOf(LiteRtEngine.DEFAULT_MAX_TOKENS) }
    var temperature by remember { mutableStateOf(LiteRtEngine.DEFAULT_SAMPLER_TEMPERATURE.toString()) }
    var topK by remember { mutableStateOf(LiteRtEngine.DEFAULT_SAMPLER_TOP_K.toString()) }
    var topP by remember { mutableStateOf(LiteRtEngine.DEFAULT_SAMPLER_TOP_P.toString()) }
    var autoWarm by remember { mutableStateOf(viewModel.autoWarmEnabled()) }
    var showClearConfirm by remember { mutableStateOf(false) }

    LaunchedEffect(Unit) {
        val cfg = viewModel.currentLocalModelConfig()
        val effWindow = clampWindow(cfg.maxTokens ?: LiteRtEngine.DEFAULT_MAX_TOKENS)
        windowValue = effWindow
        appliedWindow = cfg.maxTokens ?: LiteRtEngine.DEFAULT_MAX_TOKENS
        temperature = (cfg.temperature ?: LiteRtEngine.DEFAULT_SAMPLER_TEMPERATURE).toString()
        topK = (cfg.topK ?: LiteRtEngine.DEFAULT_SAMPLER_TOP_K).toString()
        topP = (cfg.topP ?: LiteRtEngine.DEFAULT_SAMPLER_TOP_P).toString()
        autoWarm = viewModel.autoWarmEnabled()
    }

    val freeRam = remember(engineState) { readFreeRam(context) }

    Scaffold(
        modifier = modifier.fillMaxSize(),
        containerColor = BbxBlack,
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(start = 16.dp, end = 16.dp, top = 100.dp, bottom = 24.dp)
                .verticalScroll(rememberScrollState()),
        ) {
            Text(
                "On-Device Model",
                style = MaterialTheme.typography.headlineMedium,
                color = BbxWhite,
            )
            Text(
                "Tune the Gemma model running on this phone.",
                style = MaterialTheme.typography.bodySmall,
                color = Neutral500,
                modifier = Modifier.padding(top = 4.dp, bottom = 20.dp),
            )

            // ── Context window ───────────────────────────────────────────
            SectionLabel("Context window")
            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
            ) {
                Text(
                    "$windowValue tokens",
                    style = MaterialTheme.typography.bodyMedium.copy(fontFamily = FontFamily.Monospace),
                    color = BbxWhite,
                )
                Text(
                    "Recommended: ${LiteRtEngine.DEFAULT_MAX_TOKENS}",
                    style = MaterialTheme.typography.labelSmall,
                    color = SolidGreen,
                )
            }
            Slider(
                value = windowValue.toFloat(),
                onValueChange = { windowValue = clampWindow(it.toInt()) },
                onValueChangeFinished = {
                    val committed = clampWindow(windowValue)
                    windowValue = committed
                    appliedWindow = committed
                    // Seam: persists the per-model maxTokens AND re-warms the engine.
                    viewModel.applyLocalModelSettings(
                        maxTokens = committed,
                        topK = null,
                        topP = null,
                        temperature = null,
                    )
                },
                valueRange = LiteRtEngine.MIN_TOKENS.toFloat()..LiteRtEngine.ABSOLUTE_MAX_TOKENS.toFloat(),
                modifier = Modifier.fillMaxWidth(),
                colors = SliderDefaults.colors(
                    thumbColor = BbxAccent,
                    activeTrackColor = BbxAccent,
                    inactiveTrackColor = Neutral300,
                ),
            )
            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
            ) {
                Text("${LiteRtEngine.MIN_TOKENS}", style = MaterialTheme.typography.labelSmall, color = Neutral500)
                Text("${LiteRtEngine.ABSOLUTE_MAX_TOKENS}", style = MaterialTheme.typography.labelSmall, color = Neutral500)
            }
            windowWarning(windowValue)?.let { warn ->
                Spacer(Modifier.height(6.dp))
                WarningBox(warn)
            }
            Spacer(Modifier.height(20.dp))

            // ── Sampler ──────────────────────────────────────────────────
            SectionLabel("Sampler")
            Text(
                "Lower temperature = more focused; higher = more varied.",
                style = MaterialTheme.typography.labelSmall,
                color = Neutral500,
                modifier = Modifier.padding(bottom = 8.dp),
            )
            NumberField("Temperature", temperature, KeyboardType.Decimal) { temperature = it }
            Spacer(Modifier.height(10.dp))
            NumberField("Top-K", topK, KeyboardType.Number) { topK = it }
            Spacer(Modifier.height(10.dp))
            NumberField("Top-P", topP, KeyboardType.Decimal) { topP = it }
            Spacer(Modifier.height(12.dp))
            Button(
                onClick = {
                    // Seam: persists the sampler trio AND re-warms (maxTokens=null
                    // leaves the window unchanged). Blank/unparseable fields fall back
                    // to the engine sampler defaults via resolveSampler.
                    viewModel.applyLocalModelSettings(
                        maxTokens = null,
                        topK = topK.trim().toIntOrNull() ?: LiteRtEngine.DEFAULT_SAMPLER_TOP_K,
                        topP = topP.trim().toFloatOrNull() ?: LiteRtEngine.DEFAULT_SAMPLER_TOP_P,
                        temperature = temperature.trim().toFloatOrNull() ?: LiteRtEngine.DEFAULT_SAMPLER_TEMPERATURE,
                    )
                },
                colors = ButtonDefaults.buttonColors(containerColor = BbxAccent),
                shape = RoundedCornerShape(RadiusSm),
            ) {
                Text("Apply sampler", color = BbxWhite)
            }
            Spacer(Modifier.height(24.dp))

            // ── Auto-warm on app open ────────────────────────────────────
            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column(Modifier.weight(1f).padding(end = 12.dp)) {
                    SectionLabel("Auto-load on app open")
                    Text(
                        "Load the model into memory when the app opens so the first " +
                            "message is instant. Off = loads on the first send instead.",
                        style = MaterialTheme.typography.labelSmall,
                        color = Neutral500,
                    )
                }
                Switch(
                    checked = autoWarm,
                    onCheckedChange = {
                        autoWarm = it
                        viewModel.setAutoWarmEnabled(it) // seam: LocalWarmPrefs
                    },
                    colors = SwitchDefaults.colors(
                        checkedThumbColor = BbxAccent,
                        checkedTrackColor = BbxAccent.copy(alpha = 0.3f),
                        uncheckedThumbColor = Neutral500,
                        uncheckedTrackColor = Neutral300,
                    ),
                )
            }
            Spacer(Modifier.height(24.dp))

            // ── Status readout (non-editable) ────────────────────────────
            SectionLabel("Status")
            StatusRow("Model", engineStatusLabel(engineState))
            if (engineState == LocalEngineState.WARMING) {
                Text(
                    "Reloading on-device model...",
                    style = MaterialTheme.typography.labelSmall,
                    color = BbxAccent,
                    modifier = Modifier.padding(top = 2.dp, bottom = 4.dp),
                )
            }
            StatusRow("Context window in use", "$appliedWindow tokens")
            StatusRow("Free memory", freeRam)
            Spacer(Modifier.height(28.dp))

            // ── Clear conversation ───────────────────────────────────────
            SectionLabel("Reset")
            Text(
                "Clear the accumulated conversation so the model starts fresh.",
                style = MaterialTheme.typography.labelSmall,
                color = Neutral500,
                modifier = Modifier.padding(bottom = 10.dp),
            )
            Button(
                onClick = { showClearConfirm = true },
                modifier = Modifier.fillMaxWidth(),
                colors = ButtonDefaults.buttonColors(containerColor = BbxAccent),
                shape = RoundedCornerShape(RadiusSm),
            ) {
                Text("Clear conversation", color = BbxWhite, fontWeight = FontWeight.SemiBold)
            }
        }
    }

    if (showClearConfirm) {
        AlertDialog(
            onDismissRequest = { showClearConfirm = false },
            containerColor = Neutral200,
            title = { Text("Clear conversation?", color = BbxWhite) },
            text = {
                Text(
                    "This erases the current conversation and the model's accumulated " +
                        "context. This can't be undone.",
                    color = BbxDim,
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    showClearConfirm = false
                    viewModel.clearLocalConversation() // seam
                }) {
                    Text("Clear", color = BbxAccent, fontWeight = FontWeight.SemiBold)
                }
            },
            dismissButton = {
                TextButton(onClick = { showClearConfirm = false }) {
                    Text("Cancel", color = Neutral500)
                }
            },
        )
    }
}

@Composable
private fun SectionLabel(text: String) {
    Text(
        text,
        style = MaterialTheme.typography.labelLarge.copy(fontWeight = FontWeight.SemiBold),
        color = BbxWhite,
        modifier = Modifier.padding(bottom = 6.dp),
    )
}

@Composable
private fun NumberField(
    label: String,
    value: String,
    keyboardType: KeyboardType,
    onValueChange: (String) -> Unit,
) {
    OutlinedTextField(
        value = value,
        onValueChange = onValueChange,
        label = { Text(label, color = Neutral500) },
        singleLine = true,
        keyboardOptions = KeyboardOptions(keyboardType = keyboardType),
        modifier = Modifier.fillMaxWidth(),
        colors = OutlinedTextFieldDefaults.colors(
            focusedBorderColor = BbxAccent,
            unfocusedBorderColor = Neutral300,
            cursorColor = BbxAccent,
            focusedTextColor = BbxWhite,
            unfocusedTextColor = BbxWhite,
            focusedLabelColor = BbxAccent,
            unfocusedLabelColor = Neutral500,
        ),
    )
}

@Composable
private fun StatusRow(label: String, value: String) {
    Row(
        Modifier.fillMaxWidth().padding(vertical = 4.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Text(label, style = MaterialTheme.typography.bodySmall, color = Neutral500)
        Text(
            value,
            style = MaterialTheme.typography.bodySmall.copy(fontWeight = FontWeight.Medium),
            color = BbxWhite,
        )
    }
}

@Composable
private fun WarningBox(text: String) {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(RadiusMd))
            .background(BbxAccent.copy(alpha = 0.10f))
            .border(1.dp, BbxAccent.copy(alpha = 0.4f), RoundedCornerShape(RadiusMd))
            .padding(10.dp),
    ) {
        Text(text, style = MaterialTheme.typography.labelSmall, color = BbxAccent)
    }
}

/**
 * Read the device's available RAM and format it for the status readout. The single
 * Android-touching path; the formatting is the pure [formatFreeRam].
 */
private fun readFreeRam(context: Context): String {
    val am = context.getSystemService(Context.ACTIVITY_SERVICE) as? ActivityManager
        ?: return "—"
    val info = ActivityManager.MemoryInfo()
    am.getMemoryInfo(info)
    return formatFreeRam(info.availMem)
}
