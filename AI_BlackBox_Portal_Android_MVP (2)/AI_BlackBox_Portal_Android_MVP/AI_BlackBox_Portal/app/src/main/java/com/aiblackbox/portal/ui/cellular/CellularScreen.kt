package com.aiblackbox.portal.ui.cellular

import android.app.Application
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
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
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aiblackbox.portal.data.api.BlackBoxApi
import androidx.compose.foundation.border
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontWeight
import com.aiblackbox.portal.ui.feedback.performPressFeedback
import com.aiblackbox.portal.ui.components.GlassCard
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.GlassBorder
import com.aiblackbox.portal.ui.theme.Neutral100
import com.aiblackbox.portal.ui.theme.Neutral200
import com.aiblackbox.portal.ui.theme.Neutral300
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.RadiusMd
import com.aiblackbox.portal.ui.theme.SolidGreen
import com.aiblackbox.portal.ui.theme.glassSurface
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

class CellularViewModel(application: Application) : AndroidViewModel(application) {
    private var api: BlackBoxApi? = null
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    private val _status = MutableStateFlow<Map<String, String>>(emptyMap())
    val status: StateFlow<Map<String, String>> = _status.asStateFlow()

    private val _atResponse = MutableStateFlow("")
    val atResponse: StateFlow<String> = _atResponse.asStateFlow()

    fun initialize(origin: String) {
        if (origin.isBlank() || api != null) return
        api = BlackBoxApi(origin)
        loadStatus()
    }

    fun loadStatus() {
        val api = api ?: return
        viewModelScope.launch {
            try {
                val response = api.get("/internet/status")
                val obj = json.parseToJsonElement(response).jsonObject
                val flat = mutableMapOf<String, String>()

                // Extract top-level state
                val state = obj["state"]?.jsonPrimitive?.content ?: "unknown"
                flat["state"] = state

                // Modem info (nested object)
                obj["modem"]?.jsonObject?.let { modem ->
                    modem["manufacturer"]?.jsonPrimitive?.content?.let { flat["manufacturer"] = it }
                    modem["model"]?.jsonPrimitive?.content?.let { flat["model"] = it }
                    modem["signal_quality"]?.jsonPrimitive?.content?.let { flat["signal"] = "$it%" }
                    modem["access_tech"]?.jsonPrimitive?.content?.let { flat["technology"] = it }
                    modem["operator_name"]?.jsonPrimitive?.content?.let { flat["operator"] = it }
                }

                // Connection info (nested object)
                obj["connection"]?.jsonObject?.let { conn ->
                    conn["ip"]?.jsonPrimitive?.content?.let { flat["ip_address"] = it }
                    conn["interface"]?.jsonPrimitive?.content?.let { flat["interface"] = it }
                }

                // Routing info (nested object)
                obj["routing"]?.jsonObject?.let { routing ->
                    routing["primary_interface"]?.jsonPrimitive?.content?.let { flat["primary_route"] = it }
                    val cellActive = routing["cellular_active"]
                    if (cellActive is JsonPrimitive) flat["cellular_active"] = cellActive.content
                }

                // Simple top-level fields
                obj["connection_name"]?.jsonPrimitive?.content?.let { flat["connection"] = it }
                val verified = obj["internet_verified"]
                if (verified is JsonPrimitive) flat["internet_verified"] = verified.content

                val uptime = obj["uptime_seconds"]?.jsonPrimitive?.content?.toDoubleOrNull()
                if (uptime != null && uptime > 0) {
                    val mins = (uptime / 60).toInt()
                    flat["uptime"] = if (mins >= 60) "${mins / 60}h ${mins % 60}m" else "${mins}m"
                }

                _status.value = flat
            } catch (_: Exception) {
            }
        }
    }

    fun reconnect() {
        val api = api ?: return
        viewModelScope.launch {
            try {
                api.post("/internet/deep-reconnect", "{}")
                loadStatus()
            } catch (_: Exception) {
            }
        }
    }

    fun speedTest() {
        val api = api ?: return
        viewModelScope.launch {
            try {
                val result = api.post("/internet/speed-test", "{}")
                _atResponse.value = result
            } catch (e: Exception) {
                _atResponse.value = "Error: ${e.message}"
            }
        }
    }

    fun sendAtCommand(cmd: String) {
        val api = api ?: return
        viewModelScope.launch {
            try {
                val result = api.post("/internet/at-command", """{"command":"$cmd"}""")
                _atResponse.value = result
            } catch (e: Exception) {
                _atResponse.value = "Error: ${e.message}"
            }
        }
    }
}

@Composable
fun CellularScreen(
    origin: String,
    modifier: Modifier = Modifier,
    viewModel: CellularViewModel = viewModel()
) {
    val status by viewModel.status.collectAsState()
    val atResponse by viewModel.atResponse.collectAsState()
    var atCommand by remember { mutableStateOf("") }

    LaunchedEffect(origin) { viewModel.initialize(origin) }

    val view = LocalView.current
    val isConnected = status["state"] == "connected"

    Column(
        modifier = modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(start = 16.dp, end = 16.dp, bottom = 16.dp, top = 100.dp)
    ) {
        Text(
            "\uD83D\uDCF6 Cellular",
            style = MaterialTheme.typography.headlineMedium.copy(fontWeight = FontWeight.Bold),
            color = BbxWhite
        )
        Spacer(Modifier.height(12.dp))

        // Status card with glass surface
        GlassCard(modifier = Modifier.fillMaxWidth()) {
            Column(modifier = Modifier.padding(14.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("Modem Status", style = MaterialTheme.typography.labelLarge, color = BbxAccent)
                    Spacer(Modifier.weight(1f))
                    Box(
                        Modifier.size(8.dp).clip(CircleShape)
                            .background(if (isConnected) SolidGreen else BbxAccent)
                    )
                    Spacer(Modifier.width(6.dp))
                    Text(
                        if (isConnected) "Connected" else "Disconnected",
                        style = MaterialTheme.typography.labelSmall,
                        color = if (isConnected) SolidGreen else BbxAccent
                    )
                }
                Spacer(Modifier.height(10.dp))
                status.forEach { (key, value) ->
                    Row(Modifier.fillMaxWidth().padding(vertical = 2.dp), horizontalArrangement = Arrangement.SpaceBetween) {
                        Text(key.replace("_", " ").replaceFirstChar { it.uppercase() },
                            style = MaterialTheme.typography.bodySmall, color = Neutral500)
                        Text(value, style = MaterialTheme.typography.bodySmall.copy(fontWeight = FontWeight.Medium), color = BbxWhite)
                    }
                }
            }
        }
        Spacer(Modifier.height(12.dp))

        // Action buttons with haptic
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(
                onClick = { view.performPressFeedback(); viewModel.reconnect() },
                colors = ButtonDefaults.buttonColors(containerColor = BbxAccent),
                shape = RoundedCornerShape(RadiusMd)
            ) { Text("Reconnect") }
            OutlinedButton(
                onClick = { view.performPressFeedback(); viewModel.speedTest() },
                colors = ButtonDefaults.outlinedButtonColors(contentColor = BbxDim),
                shape = RoundedCornerShape(RadiusMd)
            ) { Text("Speed Test") }
            OutlinedButton(
                onClick = { view.performPressFeedback(); viewModel.loadStatus() },
                colors = ButtonDefaults.outlinedButtonColors(contentColor = BbxDim),
                shape = RoundedCornerShape(RadiusMd)
            ) { Text("Refresh") }
        }
        Spacer(Modifier.height(16.dp))

        // AT Console with glass surface
        Text("AT Console", style = MaterialTheme.typography.labelLarge, color = BbxAccent)
        Spacer(Modifier.height(8.dp))
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Box(
                modifier = Modifier.weight(1f)
                    .glassSurface(shape = RoundedCornerShape(RadiusMd), bg = Neutral100)
                    .padding(10.dp)
            ) {
                if (atCommand.isEmpty()) {
                    Text("AT+...", color = Neutral500, style = MaterialTheme.typography.bodyMedium)
                }
                BasicTextField(
                    value = atCommand, onValueChange = { atCommand = it },
                    modifier = Modifier.fillMaxWidth(),
                    textStyle = MaterialTheme.typography.bodyMedium.copy(color = BbxWhite, fontFamily = FontFamily.Monospace),
                    cursorBrush = SolidColor(BbxAccent), singleLine = true
                )
            }
            Spacer(Modifier.width(8.dp))
            Button(
                onClick = {
                    if (atCommand.isNotBlank()) {
                        view.performPressFeedback()
                        viewModel.sendAtCommand(atCommand); atCommand = ""
                    }
                },
                colors = ButtonDefaults.buttonColors(containerColor = BbxAccent),
                shape = RoundedCornerShape(RadiusMd)
            ) { Text("Send") }
        }
        if (atResponse.isNotBlank()) {
            Spacer(Modifier.height(8.dp))
            Box(
                Modifier.fillMaxWidth()
                    .glassSurface(shape = RoundedCornerShape(RadiusMd), bg = Neutral100)
                    .border(1.dp, SolidGreen.copy(alpha = 0.2f), RoundedCornerShape(RadiusMd))
                    .padding(10.dp)
            ) {
                Text(atResponse, style = MaterialTheme.typography.bodySmall.copy(fontFamily = FontFamily.Monospace), color = SolidGreen)
            }
        }
    }
}
