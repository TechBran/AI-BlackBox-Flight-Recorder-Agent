package com.aiblackbox.portal.ui.components

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.IntRect
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.LayoutDirection
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.PopupPositionProvider
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.voice.SttEvent
import com.aiblackbox.portal.data.voice.SttFrames
import com.aiblackbox.portal.data.voice.SttProviderChoice
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject

// Live-dictation status for the two native mics (chat composer, CLI terminal), audit rank 52:
// the provider_override chip, the on-box warming state, and the switch-provider prompt after a
// switchable stt_error. The Portal (and so the WebView) renders the same frames in
// Portal/modules/stt-stream.js; tests/fixtures/stt_ws_frames.json pins both.

/** GET /stt/catalog, or null when the box does not answer. */
suspend fun fetchSttCatalog(api: BlackBoxApi): JsonObject? = try {
    Json.parseToJsonElement(api.get("/stt/catalog")).jsonObject
} catch (e: Exception) {
    android.util.Log.w("SttStatusUi", "/stt/catalog unavailable: ${e.message}")
    null
}

/** Places a popup centered above its anchor (gap [gapPx]), kept inside the window. */
class AboveAnchorPositionProvider(private val gapPx: Int) : PopupPositionProvider {
    override fun calculatePosition(
        anchorBounds: IntRect,
        windowSize: IntSize,
        layoutDirection: LayoutDirection,
        popupContentSize: IntSize,
    ): IntOffset {
        val x = anchorBounds.left + (anchorBounds.width - popupContentSize.width) / 2
        val y = anchorBounds.top - popupContentSize.height - gapPx
        val maxX = (windowSize.width - popupContentSize.width).coerceAtLeast(0)
        return IntOffset(x.coerceIn(0, maxX), y.coerceAtLeast(0))
    }
}

/** A small pill naming what the mic is doing (provider override, on-box warm-up). */
@Composable
fun SttStatusChip(text: String, modifier: Modifier = Modifier) {
    Surface(
        shape = RoundedCornerShape(10.dp),
        color = MaterialTheme.colorScheme.surface,
        contentColor = MaterialTheme.colorScheme.onSurface,
        tonalElevation = 3.dp,
        shadowElevation = 6.dp,
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
        modifier = modifier.widthIn(max = 320.dp),
    ) {
        Text(
            text = text,
            style = MaterialTheme.typography.bodySmall,
            modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
        )
    }
}

/**
 * After a switchable stt_error: offer the providers /stt/catalog lists as available and able to
 * stream, except the one that failed. Dismisses itself when there is nothing to offer (the error
 * toast already said why).
 */
@Composable
fun SttSwitchProviderDialog(
    api: BlackBoxApi,
    error: SttEvent.Error,
    onPick: (SttProviderChoice) -> Unit,
    onDismiss: () -> Unit,
) {
    var choices by remember(error) { mutableStateOf<List<SttProviderChoice>?>(null) }
    var labels by remember(error) { mutableStateOf<Map<String, String>>(emptyMap()) }
    LaunchedEffect(error) {
        val catalog = fetchSttCatalog(api)
        labels = SttFrames.labels(catalog)
        choices = SttFrames.switchAlternatives(catalog, error.provider)
    }
    val offered = choices ?: return
    if (offered.isEmpty()) {
        LaunchedEffect(Unit) { onDismiss() }
        return
    }
    val failed = error.provider?.let { labels[it] ?: it } ?: "The selected provider"
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Switch speech-to-text?") },
        text = {
            Column {
                Text("$failed can't take dictation right now (${error.message}). Dictate with:")
                offered.forEach { choice ->
                    TextButton(onClick = { onPick(choice) }) { Text(choice.label) }
                }
            }
        },
        confirmButton = {},
        dismissButton = { TextButton(onClick = onDismiss) { Text("Not now") } },
    )
}
