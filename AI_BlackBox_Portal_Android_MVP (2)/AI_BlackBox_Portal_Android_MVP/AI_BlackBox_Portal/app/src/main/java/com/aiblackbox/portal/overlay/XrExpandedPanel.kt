package com.aiblackbox.portal.overlay

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
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch

// Color constants
private val BbxRed = Color(0xFFE10600)
private val DarkSurface = Color(0xFF1A1A1A)
private val PanelBackground = Color(0xCC1A1A1A)
private val DeepSurface = Color(0xFF121212)
private val ConnectedGreen = Color(0xFF4CAF50)
private val AiBlue = Color(0xFF42A5F5)
private val MutedGray = Color(0xFF666666)
private val LabelGray = Color(0xFF888888)
private val OrbiterBackground = Color(0xB3141414)

@Composable
fun XrExpandedPanel(state: OverlayBridge.OverlayState) {
    val feedback = rememberPressFeedback()
    Column(
        verticalArrangement = Arrangement.spacedBy(6.dp),
        modifier = Modifier
            .width(420.dp)
            .clip(RoundedCornerShape(16.dp))
            .background(PanelBackground)
            .border(2.dp, BbxRed, RoundedCornerShape(16.dp))
            .padding(12.dp)
    ) {
        // ---- (M6.3) AI remote-control consent banner + kill switch ----
        // Rendered in-headset (the XR compositor doesn't surface the phone's system-overlay
        // banner). Driven by RemoteSessionBus via OverlayBridge.controlSessionActive.
        if (state.controlSessionActive) {
            ControlBanner()
        }

        // ---- a. Status Bar ----
        StatusBar(state)

        // ---- b. Selector Row ----
        SelectorRow(state)

        // ---- c. Progress Indicator ----
        if (state.isConnecting) {
            LinearProgressIndicator(
                color = BbxRed,
                trackColor = DarkSurface,
                modifier = Modifier.fillMaxWidth()
            )
        }

        // ---- d. Action Buttons ----
        ActionButtons(state)

        // ---- e. Live Response Text ----
        if (state.liveResponseText.isNotEmpty()) {
            LiveResponseSection(state.liveResponseText)
        }

        // ---- f. Transcript Section ----
        TranscriptSection(entries = state.transcriptEntries)

        // ---- g. Bottom Bar ----
        Button(
            onClick = { feedback(); OverlayBridge.openPortal() },
            colors = ButtonDefaults.buttonColors(containerColor = BbxRed),
            shape = RoundedCornerShape(8.dp),
            modifier = Modifier.fillMaxWidth().height(40.dp)
        ) {
            Text(
                text = "Full Portal",
                color = Color.White,
                fontWeight = FontWeight.Bold,
                fontSize = 13.sp
            )
        }
    }
}

// ---------- (M6.3) Remote-control consent banner ----------

/**
 * The in-headset "AI is controlling this device" consent banner + STOP kill switch, shown at
 * the top of the expanded panel while a remote-control session is actuating this headset. STOP
 * routes through [OverlayBridge.stopRemoteControl] → the Service → `RemoteSessionBus.stop()`
 * (clears the session + refuses subsequent action frames). This is the XR-native equivalent of
 * the phone's system-overlay banner / the always-on STOP notification.
 */
@Composable
private fun ControlBanner() {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .background(Color(0xEEB00020)) // strong red, mostly opaque (matches the phone banner)
            .padding(horizontal = 12.dp, vertical = 8.dp)
    ) {
        Box(
            modifier = Modifier
                .size(8.dp)
                .clip(CircleShape)
                .background(Color.White)
        )
        Spacer(modifier = Modifier.width(8.dp))
        Text(
            text = "AI is controlling this device",
            color = Color.White,
            fontSize = 13.sp,
            fontWeight = FontWeight.Bold,
            modifier = Modifier.weight(1f)
        )
        Spacer(modifier = Modifier.width(8.dp))
        Box(
            contentAlignment = Alignment.Center,
            modifier = Modifier
                .clip(RoundedCornerShape(8.dp))
                .background(Color.White)
                .clickFeedback { OverlayBridge.stopRemoteControl() }
                .padding(horizontal = 18.dp, vertical = 6.dp)
        ) {
            Text(
                text = "STOP",
                color = Color(0xFFB00020),
                fontSize = 13.sp,
                fontWeight = FontWeight.Bold
            )
        }
    }
}

// ---------- Status Bar ----------

@Composable
private fun StatusBar(state: OverlayBridge.OverlayState) {
    val feedback = rememberPressFeedback()
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier.fillMaxWidth().height(48.dp)
    ) {
        // Connection indicator dot
        Box(
            modifier = Modifier
                .size(8.dp)
                .clip(CircleShape)
                .background(if (state.isConnected) ConnectedGreen else MutedGray)
        )

        Spacer(modifier = Modifier.width(8.dp))

        Text(
            text = state.statusText,
            color = Color.White,
            fontSize = 14.sp
        )

        Spacer(modifier = Modifier.weight(1f))

        // Minimize button
        IconButton(
            onClick = { feedback(); OverlayBridge.minimize() },
            modifier = Modifier.size(32.dp)
        ) {
            Text(
                text = "X",
                color = Color.White,
                fontSize = 16.sp,
                fontWeight = FontWeight.Bold,
                textAlign = TextAlign.Center
            )
        }
    }
}

// ---------- Selector Row ----------

@Composable
private fun SelectorRow(state: OverlayBridge.OverlayState) {
    Row(
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        modifier = Modifier.fillMaxWidth()
    ) {
        DropdownSelector(
            label = "Model",
            current = state.currentModel,
            options = state.models,
            onSelect = { OverlayBridge.selectModel(it) },
            modifier = Modifier.weight(1f)
        )
        DropdownSelector(
            label = "Voice",
            current = state.currentVoice,
            options = state.voices,
            onSelect = { OverlayBridge.selectVoice(it) },
            modifier = Modifier.weight(1f)
        )
        DropdownSelector(
            label = "Operator",
            current = state.currentOperator,
            options = state.operators,
            onSelect = { OverlayBridge.selectOperator(it) },
            modifier = Modifier.weight(1f)
        )
    }
}

@Composable
private fun DropdownSelector(
    label: String,
    current: String,
    options: List<String>,
    onSelect: (String) -> Unit,
    modifier: Modifier = Modifier
) {
    var expanded by remember { mutableStateOf(false) }
    val feedback = rememberPressFeedback()

    Box(modifier = modifier) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier
                .fillMaxWidth()
                .clip(RoundedCornerShape(8.dp))
                .background(DarkSurface)
                .clickFeedback { expanded = true }
                .padding(horizontal = 8.dp, vertical = 6.dp)
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = label,
                    color = LabelGray,
                    fontSize = 9.sp
                )
                Text(
                    text = current,
                    color = Color.White,
                    fontSize = 12.sp,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
            }
            Text(
                text = "v",
                color = LabelGray,
                fontSize = 10.sp
            )
        }

        DropdownMenu(
            expanded = expanded,
            onDismissRequest = { expanded = false }
        ) {
            options.forEach { option ->
                DropdownMenuItem(
                    text = {
                        Text(
                            text = option,
                            color = if (option == current) BbxRed else Color.White,
                            fontSize = 13.sp
                        )
                    },
                    onClick = {
                        feedback()
                        onSelect(option)
                        expanded = false
                    }
                )
            }
        }
    }
}

// ---------- Action Buttons ----------

@Composable
private fun ActionButtons(state: OverlayBridge.OverlayState) {
    Row(
        horizontalArrangement = Arrangement.SpaceEvenly,
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier.fillMaxWidth().height(80.dp)
    ) {
        // Screen Share
        ActionButton(
            iconText = "SHARE",
            label = "Screen",
            borderColor = if (state.isScreenSharing) ConnectedGreen else MutedGray,
            size = 64,
            alpha = 1f,
            onClick = { OverlayBridge.toggleScreenShare() }
        )
        // Camera
        ActionButton(
            iconText = "CAM",
            label = "Camera",
            borderColor = MutedGray,
            size = 64,
            alpha = 1f,
            onClick = { OverlayBridge.openCamera() }
        )
        // Mic (larger)
        ActionButton(
            iconText = "MIC",
            label = "Mic",
            borderColor = if (state.isRecording) BbxRed else MutedGray,
            size = 72,
            alpha = if (state.isUserMuted) 0.4f else 1f,
            onClick = { OverlayBridge.toggleMic() }
        )
        // Connect / End
        ActionButton(
            iconText = if (state.isConnected) "END" else "GO",
            label = if (state.isConnected) "End" else "Connect",
            borderColor = if (state.isConnected) ConnectedGreen else MutedGray,
            size = 64,
            alpha = 1f,
            onClick = { OverlayBridge.toggleConnect() }
        )
    }
}

@Composable
private fun ActionButton(
    iconText: String,
    label: String,
    borderColor: Color,
    size: Int,
    alpha: Float,
    onClick: () -> Unit
) {
    Column(
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Box(
            contentAlignment = Alignment.Center,
            modifier = Modifier
                .size(size.dp)
                .clip(CircleShape)
                .background(DarkSurface)
                .border(2.dp, borderColor, CircleShape)
                .clickFeedback { onClick() }
        ) {
            Text(
                text = iconText,
                color = Color.White.copy(alpha = alpha),
                fontSize = if (size > 64) 16.sp else 14.sp,
                fontWeight = FontWeight.Bold,
                textAlign = TextAlign.Center
            )
        }
        Spacer(modifier = Modifier.height(2.dp))
        Text(
            text = label,
            color = LabelGray,
            fontSize = 10.sp,
            textAlign = TextAlign.Center
        )
    }
}

// ---------- Live Response ----------

@Composable
private fun LiveResponseSection(text: String) {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(8.dp))
            .background(DeepSurface)
            .padding(12.dp)
    ) {
        Text(
            text = text,
            color = Color.White,
            fontSize = 13.sp,
            maxLines = 3,
            overflow = TextOverflow.Ellipsis
        )
    }
}

// ---------- Transcript Section ----------

@Composable
private fun TranscriptSection(
    entries: List<OverlayBridge.TranscriptEntry>,
    modifier: Modifier = Modifier
) {
    var isTranscriptExpanded by remember { mutableStateOf(false) }
    val listState = rememberLazyListState()
    val coroutineScope = rememberCoroutineScope()

    Column(modifier = modifier) {
        // Header row
        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier
                .fillMaxWidth()
                .clickFeedback { isTranscriptExpanded = !isTranscriptExpanded }
                .padding(vertical = 4.dp)
        ) {
            Text(
                text = "Transcript",
                color = Color.White,
                fontSize = 13.sp,
                fontWeight = FontWeight.SemiBold
            )
            Spacer(modifier = Modifier.width(6.dp))
            Text(
                text = "(${entries.size})",
                color = LabelGray,
                fontSize = 11.sp
            )
            Spacer(modifier = Modifier.weight(1f))
            Text(
                text = if (isTranscriptExpanded) "^" else "v",
                color = LabelGray,
                fontSize = 12.sp
            )
        }

        // Transcript list
        if (isTranscriptExpanded) {
            LazyColumn(
                state = listState,
                modifier = Modifier
                    .fillMaxWidth()
                    .height(200.dp)
                    .clip(RoundedCornerShape(8.dp))
                    .background(DeepSurface)
                    .padding(8.dp)
            ) {
                items(entries) { entry ->
                    TranscriptEntryRow(entry)
                    Spacer(modifier = Modifier.height(4.dp))
                }
            }

            // Auto-scroll to bottom when new entries arrive
            LaunchedEffect(entries.size) {
                if (entries.isNotEmpty()) {
                    coroutineScope.launch {
                        listState.animateScrollToItem(entries.size - 1)
                    }
                }
            }
        }
    }
}

@Composable
private fun TranscriptEntryRow(entry: OverlayBridge.TranscriptEntry) {
    Column(modifier = Modifier.fillMaxWidth()) {
        Text(
            text = entry.speaker,
            color = if (entry.speaker.equals("User", ignoreCase = true)) ConnectedGreen else AiBlue,
            fontSize = 10.sp,
            fontWeight = FontWeight.Bold
        )
        Text(
            text = entry.text,
            color = Color.White,
            fontSize = 12.sp
        )
    }
}

// ============================================================
// XrOrbiterControls — floating bar beneath the panel
// ============================================================

@Composable
fun XrOrbiterControls(state: OverlayBridge.OverlayState) {
    Row(
        horizontalArrangement = Arrangement.SpaceEvenly,
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier
            .width(240.dp)
            .height(56.dp)
            .clip(RoundedCornerShape(28.dp))
            .background(OrbiterBackground)
            .padding(horizontal = 8.dp)
    ) {
        // Mic toggle
        Box(
            contentAlignment = Alignment.Center,
            modifier = Modifier
                .size(40.dp)
                .clip(CircleShape)
                .background(DarkSurface)
                .clickFeedback { OverlayBridge.toggleMic() }
        ) {
            Text(
                text = "MIC",
                color = if (state.isRecording) BbxRed else Color.White,
                fontSize = 11.sp,
                fontWeight = FontWeight.Bold,
                textAlign = TextAlign.Center
            )
        }

        // Minimize
        Box(
            contentAlignment = Alignment.Center,
            modifier = Modifier
                .size(40.dp)
                .clip(CircleShape)
                .background(DarkSurface)
                .clickFeedback { OverlayBridge.minimize() }
        ) {
            Text(
                text = "MIN",
                color = Color.White,
                fontSize = 11.sp,
                fontWeight = FontWeight.Bold,
                textAlign = TextAlign.Center
            )
        }

        // End session
        Box(
            contentAlignment = Alignment.Center,
            modifier = Modifier
                .size(40.dp)
                .clip(CircleShape)
                .background(DarkSurface)
                .clickFeedback { OverlayBridge.toggleConnect() }
        ) {
            Text(
                text = "END",
                color = BbxRed,
                fontSize = 11.sp,
                fontWeight = FontWeight.Bold,
                textAlign = TextAlign.Center
            )
        }
    }
}
