package com.aiblackbox.portal.ui.telephony

import android.app.Application
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.view.HapticFeedbackConstants
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import com.aiblackbox.portal.ui.feedback.clickFeedback
import com.aiblackbox.portal.ui.feedback.performPressFeedback
import com.aiblackbox.portal.ui.feedback.rememberPressFeedback
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.ui.components.GlassCard
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.Neutral100
import com.aiblackbox.portal.ui.theme.Neutral200
import com.aiblackbox.portal.ui.theme.Neutral500
import com.aiblackbox.portal.ui.theme.RadiusMd
import com.aiblackbox.portal.ui.theme.SolidGreen
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

// =============================================================================
// Data model (v2 /asterisk/gateways)
// =============================================================================

data class GwPort(
    val span: Int?,
    val slot: Int?,
    val phoneNumber: String,
    val carrier: String,
    val enabled: Boolean,
    val operator: String
)

data class SimSlot(
    val span: Int?,
    val slot: Int?,
    val carrier: String,
    val signal: Int?,
    val registered: Boolean,
    val phoneNumber: String,
    val status: String
)

data class Gateway(
    val id: String,
    val name: String,
    val ip: String,
    val model: String,
    val enabled: Boolean,
    val sipPort: Int,
    val httpPort: Int,
    val codec: String,
    val httpUser: String,
    val hasHttpPassword: Boolean,
    val amiUser: String,
    val hasAmiSecret: Boolean,
    val reachable: Boolean,
    val sipRegistered: Boolean,
    val ports: List<GwPort>,
    val simSlots: List<SimSlot>
) {
    val displayStatus: String
        get() = when {
            reachable && sipRegistered -> "online"
            reachable -> "not registered"
            else -> "offline"
        }
}

// Validation result (POST /validate)
data class ValidateResult(
    val reachable: Boolean,
    val amiAuth: Boolean,
    val trunkOnline: Boolean,
    val spans: List<SimSlot>
)

// Config preview (GET /config-preview)
data class ConfigPreview(
    val asteriskConf: String,
    val tgSteps: List<String>
)

// Apply result (POST /apply)
data class ApplyResult(
    val applied: Boolean,
    val reloadOk: Boolean,
    val restartRecommended: Boolean
)

// Models and GSM port counts (mirrors backend MODEL_PORTS).
val MODEL_PORTS = linkedMapOf("TG100" to 1, "TG200" to 2, "TG400" to 4, "TG800" to 8)

// =============================================================================
// ViewModel
// =============================================================================

class TelephonyViewModel(application: Application) : AndroidViewModel(application) {
    private var api: BlackBoxApi? = null
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    private val _gateways = MutableStateFlow<List<Gateway>>(emptyList())
    val gateways: StateFlow<List<Gateway>> = _gateways.asStateFlow()

    private val _isLoading = MutableStateFlow(false)
    val isLoading: StateFlow<Boolean> = _isLoading.asStateFlow()

    private val _operators = MutableStateFlow<List<String>>(emptyList())
    val operators: StateFlow<List<String>> = _operators.asStateFlow()

    private val _discovered = MutableStateFlow<List<Pair<String, String>>>(emptyList()) // ip to model
    val discovered: StateFlow<List<Pair<String, String>>> = _discovered.asStateFlow()

    private val _actionMessage = MutableStateFlow<String?>(null)
    val actionMessage: StateFlow<String?> = _actionMessage.asStateFlow()

    private val _isDiscovering = MutableStateFlow(false)
    val isDiscovering: StateFlow<Boolean> = _isDiscovering.asStateFlow()

    // Wizard state
    private val _validateResult = MutableStateFlow<ValidateResult?>(null)
    val validateResult: StateFlow<ValidateResult?> = _validateResult.asStateFlow()

    private val _configPreview = MutableStateFlow<ConfigPreview?>(null)
    val configPreview: StateFlow<ConfigPreview?> = _configPreview.asStateFlow()

    private val _applyResult = MutableStateFlow<ApplyResult?>(null)
    val applyResult: StateFlow<ApplyResult?> = _applyResult.asStateFlow()

    private val _wizardBusy = MutableStateFlow(false)
    val wizardBusy: StateFlow<Boolean> = _wizardBusy.asStateFlow()

    fun initialize(origin: String) {
        if (origin.isBlank() || api != null) return
        api = BlackBoxApi(origin)
        loadGateways()
        loadOperators()
    }

    private fun JsonObject.str(key: String): String? = this[key]?.jsonPrimitive?.content
    private fun JsonObject.bool(key: String): Boolean =
        this[key]?.jsonPrimitive?.booleanOrNull
            ?: this[key]?.jsonPrimitive?.content?.toBooleanStrictOrNull() ?: false
    private fun JsonObject.intOr(key: String, default: Int): Int =
        this[key]?.jsonPrimitive?.intOrNull
            ?: this[key]?.jsonPrimitive?.content?.toIntOrNull() ?: default
    private fun JsonObject.intOrNullField(key: String): Int? =
        this[key]?.jsonPrimitive?.intOrNull
            ?: this[key]?.jsonPrimitive?.content?.toIntOrNull()

    fun loadGateways() {
        val api = api ?: return
        _isLoading.value = true
        viewModelScope.launch {
            try {
                val response = api.get("/asterisk/gateways")
                val root = json.parseToJsonElement(response).jsonObject
                val arr = root["gateways"]?.jsonArray ?: return@launch
                _gateways.value = arr.mapNotNull { el ->
                    try { parseGateway(el.jsonObject) } catch (_: Exception) { null }
                }
            } catch (_: Exception) {
                _gateways.value = emptyList()
            } finally {
                _isLoading.value = false
            }
        }
    }

    private fun parseGateway(obj: JsonObject): Gateway? {
        val id = obj.str("id") ?: return null
        val name = obj.str("name") ?: id
        val ip = obj.str("ip") ?: obj.str("host") ?: ""
        val model = obj.str("model") ?: "TG200"
        val enabled = obj.bool("enabled")
        val sipPort = obj.intOr("sip_port", 5060)
        val httpPort = obj.intOr("http_port", 80)
        val codec = obj.str("codec") ?: "g711"

        val httpObj = obj["http"]?.jsonObject
        val httpUser = httpObj?.str("user") ?: ""
        val hasHttpPassword = httpObj?.bool("has_password") ?: false

        val amiObj = obj["ami"]?.jsonObject
        val amiUser = amiObj?.str("user") ?: ""
        val hasAmiSecret = amiObj?.bool("has_secret") ?: false

        val statusObj = obj["status"]?.jsonObject
        val reachable = statusObj?.bool("reachable") ?: false
        val sipRegistered = statusObj?.bool("sip_registered") ?: false

        val ports = obj["ports"]?.jsonArray?.mapNotNull { pEl ->
            try {
                val p = pEl.jsonObject
                GwPort(
                    span = p.intOrNullField("span"),
                    slot = p.intOrNullField("slot"),
                    phoneNumber = p.str("phone_number") ?: "",
                    carrier = p.str("carrier") ?: "",
                    enabled = p["enabled"]?.let { p.bool("enabled") } ?: true,
                    operator = p.str("operator") ?: ""
                )
            } catch (_: Exception) { null }
        } ?: emptyList()

        val simSlots = statusObj?.get("sim_slots")?.jsonArray?.mapNotNull { sEl ->
            try {
                val s = sEl.jsonObject
                SimSlot(
                    span = s.intOrNullField("span"),
                    slot = s.intOrNullField("slot"),
                    carrier = s.str("carrier") ?: "",
                    signal = s.intOrNullField("signal"),
                    registered = s.bool("registered"),
                    phoneNumber = s.str("phone_number") ?: "",
                    status = s.str("status") ?: ""
                )
            } catch (_: Exception) { null }
        } ?: emptyList()

        return Gateway(
            id = id, name = name, ip = ip, model = model, enabled = enabled,
            sipPort = sipPort, httpPort = httpPort, codec = codec,
            httpUser = httpUser, hasHttpPassword = hasHttpPassword,
            amiUser = amiUser, hasAmiSecret = hasAmiSecret,
            reachable = reachable, sipRegistered = sipRegistered,
            ports = ports, simSlots = simSlots
        )
    }

    fun loadOperators() {
        val api = api ?: return
        viewModelScope.launch {
            try {
                val response = api.get("/health")
                val root = json.parseToJsonElement(response).jsonObject
                val list = root["users"]?.jsonObject?.get("list")?.jsonArray
                _operators.value = list?.mapNotNull { it.jsonPrimitive.content } ?: emptyList()
            } catch (_: Exception) {
                _operators.value = emptyList()
            }
        }
    }

    fun discover() {
        val api = api ?: return
        if (_isDiscovering.value) return
        _isDiscovering.value = true
        viewModelScope.launch {
            try {
                val response = api.post("/asterisk/gateways/discover", "{}")
                val root = json.parseToJsonElement(response).jsonObject
                val arr = root["discovered"]?.jsonArray ?: root["gateways"]?.jsonArray
                val existingIPs = _gateways.value.map { it.ip }.toSet()
                val found = arr?.mapNotNull { el ->
                    val o = el.jsonObject
                    val ip = o.str("ip") ?: return@mapNotNull null
                    if (ip in existingIPs) return@mapNotNull null
                    ip to (o.str("model") ?: "TG200")
                } ?: emptyList()
                _discovered.value = found
                _actionMessage.value = if (found.isEmpty())
                    "No new gateways found" else "Found ${found.size} gateway(s)"
            } catch (e: Exception) {
                _actionMessage.value = "Discovery failed: ${e.message}"
            } finally {
                _isDiscovering.value = false
            }
        }
    }

    fun clearDiscovered() { _discovered.value = emptyList() }

    fun deleteGateway(id: String) {
        val api = api ?: return
        viewModelScope.launch {
            try {
                api.delete("/asterisk/gateways/$id")
                _actionMessage.value = "Gateway removed"
                loadGateways()
            } catch (e: Exception) {
                _actionMessage.value = "Failed to remove: ${e.message}"
            }
        }
    }

    fun addGateway(payloadJson: String) {
        val api = api ?: return
        viewModelScope.launch {
            try {
                api.post("/asterisk/gateways", payloadJson)
                _actionMessage.value = "Gateway added"
                loadGateways()
            } catch (e: Exception) {
                _actionMessage.value = "Failed to add: ${e.message}"
            }
        }
    }

    fun updateGateway(id: String, payloadJson: String) {
        val api = api ?: return
        viewModelScope.launch {
            try {
                api.put("/asterisk/gateways/$id", payloadJson)
                _actionMessage.value = "Gateway updated"
                loadGateways()
            } catch (e: Exception) {
                _actionMessage.value = "Failed to update: ${e.message}"
            }
        }
    }

    // ── Wizard ──

    fun resetWizard() {
        _validateResult.value = null
        _configPreview.value = null
        _applyResult.value = null
        _wizardBusy.value = false
    }

    fun validate(id: String) {
        val api = api ?: return
        _wizardBusy.value = true
        viewModelScope.launch {
            try {
                val response = api.post("/asterisk/gateways/$id/validate", "{}")
                val root = json.parseToJsonElement(response).jsonObject
                val spans = root["spans"]?.jsonArray?.mapNotNull { sEl ->
                    try {
                        val s = sEl.jsonObject
                        SimSlot(
                            span = s.intOrNullField("span"),
                            slot = s.intOrNullField("slot"),
                            carrier = s.str("carrier") ?: "",
                            signal = s.intOrNullField("signal"),
                            registered = s.bool("registered"),
                            phoneNumber = s.str("phone_number") ?: "",
                            status = s.str("status") ?: ""
                        )
                    } catch (_: Exception) { null }
                } ?: emptyList()
                _validateResult.value = ValidateResult(
                    reachable = root.bool("reachable"),
                    amiAuth = root.bool("ami_auth"),
                    trunkOnline = root.bool("trunk_online"),
                    spans = spans
                )
            } catch (e: Exception) {
                _actionMessage.value = "Validation failed: ${e.message}"
                _validateResult.value = null
            } finally {
                _wizardBusy.value = false
            }
        }
    }

    fun configPreview(id: String) {
        val api = api ?: return
        _wizardBusy.value = true
        viewModelScope.launch {
            try {
                val response = api.get("/asterisk/gateways/$id/config-preview")
                val root = json.parseToJsonElement(response).jsonObject
                val steps = root["tg_steps"]?.jsonArray?.mapNotNull { it.jsonPrimitive.content } ?: emptyList()
                _configPreview.value = ConfigPreview(
                    asteriskConf = root.str("asterisk_conf") ?: "",
                    tgSteps = steps
                )
            } catch (e: Exception) {
                _actionMessage.value = "Failed to load preview: ${e.message}"
                _configPreview.value = null
            } finally {
                _wizardBusy.value = false
            }
        }
    }

    fun apply(id: String) {
        val api = api ?: return
        _wizardBusy.value = true
        viewModelScope.launch {
            try {
                val response = api.post("/asterisk/gateways/$id/apply", "{}")
                val root = json.parseToJsonElement(response).jsonObject
                val reload = root["reload"]?.jsonObject
                _applyResult.value = ApplyResult(
                    applied = root.bool("applied"),
                    reloadOk = reload?.bool("ok") ?: false,
                    restartRecommended = root.bool("restart_recommended")
                )
                _actionMessage.value = if (root.bool("applied")) "Config applied" else "Config processed"
            } catch (e: Exception) {
                _actionMessage.value = "Failed to apply: ${e.message}"
            } finally {
                _wizardBusy.value = false
            }
        }
    }

    fun clearActionMessage() { _actionMessage.value = null }
}

// =============================================================================
// JSON payload builder (send-on-change secrets)
// =============================================================================

private fun jsonStr(s: String): String =
    "\"" + s.replace("\\", "\\\\").replace("\"", "\\\"") + "\""

/**
 * Build the add/edit payload. Secrets (httpPassword/amiSecret) are included ONLY
 * when non-blank (send-on-change). Mirrors web handleFormSave.
 */
fun buildGatewayPayload(
    name: String,
    ip: String,
    model: String,
    sipPort: Int,
    httpPort: Int,
    codec: String,
    httpUser: String,
    httpPassword: String,
    amiUser: String,
    amiSecret: String,
    ports: List<GwPort>,
    operator: String
): String {
    val sb = StringBuilder()
    sb.append("{")
    sb.append("\"name\":").append(jsonStr(name))
    sb.append(",\"ip\":").append(jsonStr(ip))
    sb.append(",\"model\":").append(jsonStr(model))
    sb.append(",\"sip_port\":").append(sipPort)
    sb.append(",\"http_port\":").append(httpPort)
    sb.append(",\"codec\":").append(jsonStr(codec))
    sb.append(",\"http_user\":").append(if (httpUser.isBlank()) "null" else jsonStr(httpUser))
    sb.append(",\"ami_user\":").append(if (amiUser.isBlank()) "null" else jsonStr(amiUser))
    sb.append(",\"operator\":").append(jsonStr(operator))
    if (httpPassword.isNotBlank()) sb.append(",\"http_password\":").append(jsonStr(httpPassword))
    if (amiSecret.isNotBlank()) sb.append(",\"ami_secret\":").append(jsonStr(amiSecret))
    if (ports.isNotEmpty()) {
        sb.append(",\"ports\":[")
        ports.forEachIndexed { i, p ->
            if (i > 0) sb.append(",")
            sb.append("{")
            sb.append("\"span\":").append(p.span ?: 0)
            if (p.slot != null) sb.append(",\"slot\":").append(p.slot)
            sb.append(",\"phone_number\":").append(jsonStr(p.phoneNumber))
            sb.append(",\"operator\":").append(jsonStr(p.operator))
            sb.append(",\"enabled\":").append(p.enabled)
            sb.append("}")
        }
        sb.append("]")
    }
    sb.append("}")
    return sb.toString()
}

private fun copyToClipboard(context: Context, text: String, label: String) {
    val cm = context.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager ?: return
    cm.setPrimaryClip(ClipData.newPlainText(label, text))
}

// =============================================================================
// Screen
// =============================================================================

@Composable
fun TelephonyScreen(
    origin: String,
    modifier: Modifier = Modifier,
    viewModel: TelephonyViewModel = viewModel()
) {
    val gateways by viewModel.gateways.collectAsState()
    val isLoading by viewModel.isLoading.collectAsState()
    val operators by viewModel.operators.collectAsState()
    val discovered by viewModel.discovered.collectAsState()
    val isDiscovering by viewModel.isDiscovering.collectAsState()
    val actionMessage by viewModel.actionMessage.collectAsState()

    LaunchedEffect(origin) { viewModel.initialize(origin) }

    val view = LocalView.current
    val context = LocalContext.current

    // Dialog state
    var showForm by remember { mutableStateOf(false) }
    var editingGateway by remember { mutableStateOf<Gateway?>(null) }
    var prefillIp by remember { mutableStateOf("") }
    var prefillModel by remember { mutableStateOf("TG200") }

    var wizardGateway by remember { mutableStateOf<Gateway?>(null) }
    var confirmRemove by remember { mutableStateOf<Gateway?>(null) }

    Column(modifier = modifier.fillMaxSize().padding(start = 16.dp, end = 16.dp, bottom = 16.dp, top = 100.dp)) {
        Row(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text(
                "📞 Telephony",
                style = MaterialTheme.typography.headlineMedium.copy(fontWeight = FontWeight.Bold),
                color = BbxWhite
            )
            Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                TextButton(onClick = {
                    view.performPressFeedback()
                    viewModel.discover()
                }) { Text(if (isDiscovering) "Scanning…" else "Discover", color = BbxAccent) }
                TextButton(onClick = {
                    view.performPressFeedback()
                    editingGateway = null
                    prefillIp = ""
                    prefillModel = "TG200"
                    showForm = true
                }) { Text("+ Add", color = SolidGreen) }
                TextButton(onClick = {
                    view.performPressFeedback()
                    viewModel.loadGateways()
                }) { Text("Refresh", color = BbxDim) }
            }
        }

        // Action message (lightweight inline toast)
        actionMessage?.let { msg ->
            Spacer(Modifier.height(8.dp))
            Box(
                Modifier.fillMaxWidth()
                    .clip(RoundedCornerShape(RadiusMd))
                    .background(Neutral200)
                    .clickFeedback { viewModel.clearActionMessage() }
                    .padding(horizontal = 12.dp, vertical = 8.dp)
            ) {
                Text(msg, color = BbxDim, style = MaterialTheme.typography.bodySmall)
            }
        }

        Spacer(Modifier.height(12.dp))

        if (isLoading && gateways.isEmpty()) {
            Box(Modifier.fillMaxWidth().padding(16.dp), contentAlignment = Alignment.Center) {
                CircularProgressIndicator(color = BbxAccent, modifier = Modifier.size(24.dp), strokeWidth = 2.dp)
            }
        }

        if (gateways.isEmpty() && discovered.isEmpty() && !isLoading) {
            Box(Modifier.fillMaxWidth().padding(32.dp), contentAlignment = Alignment.Center) {
                Text(
                    "No gateways configured. Tap Discover to scan or + Add to configure manually.",
                    color = Neutral500,
                    style = MaterialTheme.typography.bodyMedium
                )
            }
        }

        LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            // Discovered (not yet added) gateways first
            items(discovered, key = { "disc-" + it.first }) { (ip, model) ->
                DiscoveredCard(
                    ip = ip,
                    model = model,
                    onAdd = {
                        editingGateway = null
                        prefillIp = ip
                        prefillModel = model
                        showForm = true
                    }
                )
            }

            items(gateways, key = { it.id }) { gw ->
                GatewayCard(
                    gw = gw,
                    onSetup = {
                        viewModel.resetWizard()
                        wizardGateway = gw
                    },
                    onEdit = {
                        editingGateway = gw
                        showForm = true
                    },
                    onRemove = {
                        confirmRemove = gw
                    }
                )
            }
        }
    }

    // Add / Edit form dialog
    if (showForm) {
        GatewayFormDialog(
            editing = editingGateway,
            prefillIp = prefillIp,
            prefillModel = prefillModel,
            operators = operators,
            onDismiss = { showForm = false },
            onSave = { payload ->
                val ed = editingGateway
                if (ed != null) viewModel.updateGateway(ed.id, payload)
                else viewModel.addGateway(payload)
                viewModel.clearDiscovered()
                showForm = false
            }
        )
    }

    // Setup wizard dialog
    wizardGateway?.let { gw ->
        SetupWizardDialog(
            gateway = gw,
            viewModel = viewModel,
            context = context,
            onClose = {
                wizardGateway = null
                viewModel.resetWizard()
            }
        )
    }

    // Remove confirmation
    confirmRemove?.let { gw ->
        ConfirmRemoveDialog(
            gatewayName = gw.name,
            onConfirm = {
                viewModel.deleteGateway(gw.id)
                confirmRemove = null
            },
            onDismiss = { confirmRemove = null }
        )
    }
}

// =============================================================================
// Gateway card
// =============================================================================

@Composable
private fun GatewayCard(
    gw: Gateway,
    onSetup: () -> Unit,
    onEdit: () -> Unit,
    onRemove: () -> Unit
) {
    val accentColor = when {
        gw.reachable && gw.sipRegistered -> SolidGreen
        gw.reachable -> Color(0xFFFFA726) // amber
        else -> BbxAccent // red
    }
    val borderColor = accentColor.copy(alpha = 0.4f)

    GlassCard(modifier = Modifier.fillMaxWidth().border(1.dp, borderColor, RoundedCornerShape(RadiusMd))) {
        Column(modifier = Modifier.padding(14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(8.dp).clip(CircleShape).background(accentColor))
                Spacer(Modifier.width(8.dp))
                Text(
                    gw.name,
                    style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.SemiBold),
                    color = BbxWhite
                )
                Spacer(Modifier.weight(1f))
                Box(
                    Modifier.clip(RoundedCornerShape(8.dp))
                        .background(accentColor.copy(alpha = 0.15f))
                        .padding(horizontal = 8.dp, vertical = 2.dp)
                ) {
                    Text(
                        gw.displayStatus.uppercase(),
                        style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Medium),
                        color = accentColor
                    )
                }
            }
            Spacer(Modifier.height(4.dp))
            Text("${gw.model} · ${gw.ip}", style = MaterialTheme.typography.bodySmall, color = Neutral500)

            // Per-SIM list
            Spacer(Modifier.height(8.dp))
            if (gw.simSlots.isEmpty()) {
                Text("SIMs: —", style = MaterialTheme.typography.labelSmall, color = Neutral500)
            } else {
                gw.simSlots.forEachIndexed { i, sim ->
                    Row(
                        Modifier.fillMaxWidth().padding(vertical = 2.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Box(
                            Modifier.size(6.dp).clip(CircleShape)
                                .background(if (sim.registered) SolidGreen else Neutral500)
                        )
                        Spacer(Modifier.width(6.dp))
                        Text(
                            "SIM ${i + 1}" + (sim.span?.let { " (span $it)" } ?: ""),
                            style = MaterialTheme.typography.labelSmall,
                            color = BbxDim,
                            modifier = Modifier.width(96.dp)
                        )
                        Text(
                            sim.carrier.ifBlank { "—" },
                            style = MaterialTheme.typography.labelSmall,
                            color = Neutral500,
                            modifier = Modifier.weight(1f)
                        )
                        Text(
                            sim.signal?.let { "$it%" } ?: "—",
                            style = MaterialTheme.typography.labelSmall,
                            color = if (sim.signal != null && sim.signal >= 40) SolidGreen else Neutral500
                        )
                    }
                    if (sim.phoneNumber.isNotBlank()) {
                        Text(
                            "    ${sim.phoneNumber}",
                            style = MaterialTheme.typography.labelSmall,
                            color = Neutral500
                        )
                    }
                }
            }

            Spacer(Modifier.height(10.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                CardActionButton("Setup", SolidGreen, onSetup)
                CardActionButton("Edit", BbxAccent, onEdit)
                CardActionButton("Remove", BbxAccent, onRemove)
            }
        }
    }
}

@Composable
private fun CardActionButton(label: String, color: Color, onClick: () -> Unit) {
    Box(
        Modifier.clip(RoundedCornerShape(RadiusMd))
            .background(color.copy(alpha = 0.1f))
            .border(1.dp, color.copy(alpha = 0.3f), RoundedCornerShape(RadiusMd))
            .clickFeedback { onClick() }
            .padding(horizontal = 12.dp, vertical = 6.dp)
    ) {
        Text(label, style = MaterialTheme.typography.labelSmall, color = color)
    }
}

@Composable
private fun DiscoveredCard(ip: String, model: String, onAdd: () -> Unit) {
    val amber = Color(0xFFFFA726)
    GlassCard(modifier = Modifier.fillMaxWidth().border(1.dp, amber.copy(alpha = 0.4f), RoundedCornerShape(RadiusMd))) {
        Column(modifier = Modifier.padding(14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(8.dp).clip(CircleShape).background(amber))
                Spacer(Modifier.width(8.dp))
                Text(model, style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.SemiBold), color = BbxWhite)
                Spacer(Modifier.weight(1f))
                Box(
                    Modifier.clip(RoundedCornerShape(8.dp)).background(amber.copy(alpha = 0.15f))
                        .padding(horizontal = 8.dp, vertical = 2.dp)
                ) {
                    Text("DISCOVERED", style = MaterialTheme.typography.labelSmall, color = amber)
                }
            }
            Spacer(Modifier.height(4.dp))
            Text(ip, style = MaterialTheme.typography.bodySmall, color = Neutral500)
            Spacer(Modifier.height(10.dp))
            CardActionButton("+ Add This Gateway", SolidGreen, onAdd)
        }
    }
}

// =============================================================================
// Add / Edit form dialog
// =============================================================================

@Composable
private fun GatewayFormDialog(
    editing: Gateway?,
    prefillIp: String,
    prefillModel: String,
    operators: List<String>,
    onDismiss: () -> Unit,
    onSave: (String) -> Unit
) {
    val isEdit = editing != null
    var name by remember { mutableStateOf(editing?.name ?: "") }
    var ip by remember { mutableStateOf(editing?.ip ?: prefillIp) }
    var model by remember { mutableStateOf(editing?.model ?: prefillModel) }
    var sipPort by remember { mutableStateOf((editing?.sipPort ?: 5060).toString()) }
    var httpPort by remember { mutableStateOf((editing?.httpPort ?: 80).toString()) }
    var codec by remember { mutableStateOf(editing?.codec ?: "g711") }
    var httpUser by remember { mutableStateOf(editing?.httpUser ?: "") }
    var httpPass by remember { mutableStateOf("") }
    var amiUser by remember { mutableStateOf(editing?.amiUser ?: "") }
    var amiSecret by remember { mutableStateOf("") }

    // Per-line editor state (only meaningful on edit, where ports exist)
    val lineStates = remember {
        mutableStateListOf<LineEditState>().apply {
            editing?.ports?.forEach { p ->
                add(LineEditState(
                    span = p.span,
                    slot = p.slot,
                    carrier = p.carrier,
                    phoneNumber = mutableStateOf(p.phoneNumber),
                    operator = mutableStateOf(p.operator),
                    enabled = mutableStateOf(p.enabled)
                ))
            }
        }
    }

    DialogScaffold(
        title = if (isEdit) "Edit Gateway" else "Add Gateway",
        onDismiss = onDismiss,
        confirmLabel = if (isEdit) "Update" else "Add Gateway",
        confirmEnabled = name.isNotBlank() && ip.isNotBlank(),
        onConfirm = {
            val ports = lineStates.map { ls ->
                GwPort(
                    span = ls.span,
                    slot = ls.slot,
                    phoneNumber = ls.phoneNumber.value.trim(),
                    carrier = ls.carrier,
                    enabled = ls.enabled.value,
                    operator = ls.operator.value
                )
            }
            val op = editing?.ports?.firstOrNull { it.operator.isNotBlank() }?.operator
                ?: operators.firstOrNull() ?: "system"
            onSave(
                buildGatewayPayload(
                    name = name.trim(),
                    ip = ip.trim(),
                    model = model,
                    sipPort = sipPort.toIntOrNull() ?: 5060,
                    httpPort = httpPort.toIntOrNull() ?: 80,
                    codec = codec,
                    httpUser = httpUser.trim(),
                    httpPassword = httpPass,
                    amiUser = amiUser.trim(),
                    amiSecret = amiSecret,
                    ports = ports,
                    operator = op
                )
            )
        }
    ) {
        FormField("Name", name, { name = it }, "e.g. Office TG200")
        FormField("IP Address", ip, { ip = it }, "e.g. 192.168.1.100")

        Spacer(Modifier.height(8.dp))
        Text("Model", style = MaterialTheme.typography.labelMedium, color = Neutral500)
        Spacer(Modifier.height(4.dp))
        FormDropdown(
            selectedLabel = MODEL_PORTS[model]?.let { c ->
                "$model ($c ${if (c == 1) "line" else "lines"})"
            } ?: model,
            options = MODEL_PORTS.keys.toList(),
            optionLabel = { m -> MODEL_PORTS[m]?.let { "$m ($it ${if (it == 1) "line" else "lines"})" } ?: m },
            isSelected = { it == model },
            onSelect = { model = it }
        )

        FormField("SIP Port", sipPort, { sipPort = it.filter { c -> c.isDigit() } }, "5060", number = true)
        FormField("HTTP Port", httpPort, { httpPort = it.filter { c -> c.isDigit() } }, "80", number = true)

        Spacer(Modifier.height(8.dp))
        Text("Codec", style = MaterialTheme.typography.labelMedium, color = Neutral500)
        Spacer(Modifier.height(4.dp))
        FormDropdown(
            selectedLabel = if (codec == "g722") "G.722 HD" else "G.711 (ulaw/alaw)",
            options = listOf("g722", "g711"),
            optionLabel = { if (it == "g722") "G.722 HD" else "G.711 (ulaw/alaw)" },
            isSelected = { it == codec },
            onSelect = { codec = it }
        )

        SectionLabel("TG web GUI (HTTP)")
        FormField("HTTP Username", httpUser, { httpUser = it }, "admin")
        FormField(
            "HTTP Password", httpPass, { httpPass = it },
            if (isEdit) "(unchanged)" else "password", password = true
        )

        SectionLabel("Asterisk Manager Interface (AMI)")
        FormField("AMI Username", amiUser, { amiUser = it }, "blackbox")
        FormField(
            "AMI Secret", amiSecret, { amiSecret = it },
            if (isEdit) "(unchanged)" else "secret", password = true
        )

        if (lineStates.isNotEmpty()) {
            SectionLabel("Lines")
            lineStates.forEachIndexed { idx, ls ->
                LineEditorRow(idx = idx, ls = ls, operators = operators)
            }
        }
    }
}

private class LineEditState(
    val span: Int?,
    val slot: Int?,
    val carrier: String,
    val phoneNumber: androidx.compose.runtime.MutableState<String>,
    val operator: androidx.compose.runtime.MutableState<String>,
    val enabled: androidx.compose.runtime.MutableState<Boolean>
)

@Composable
private fun LineEditorRow(idx: Int, ls: LineEditState, operators: List<String>) {
    val feedback = rememberPressFeedback()
    val lineNum = (ls.slot ?: ((ls.span ?: 3) - 2)) + 1
    Column(
        Modifier.fillMaxWidth().padding(vertical = 6.dp)
            .clip(RoundedCornerShape(RadiusMd)).background(Neutral100).padding(10.dp)
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                "Line $lineNum" + (ls.span?.let { " · span $it" } ?: ""),
                style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.SemiBold),
                color = BbxDim,
                modifier = Modifier.weight(1f)
            )
            if (ls.carrier.isNotBlank()) {
                Text(ls.carrier, style = MaterialTheme.typography.labelSmall, color = Neutral500)
            }
            Spacer(Modifier.width(8.dp))
            Switch(
                checked = ls.enabled.value,
                onCheckedChange = { feedback(); ls.enabled.value = it },
                colors = SwitchDefaults.colors(
                    checkedThumbColor = BbxWhite,
                    checkedTrackColor = SolidGreen,
                    uncheckedThumbColor = BbxDim,
                    uncheckedTrackColor = Neutral200
                )
            )
        }
        Spacer(Modifier.height(6.dp))
        OutlinedTextField(
            value = ls.phoneNumber.value,
            onValueChange = { ls.phoneNumber.value = it },
            modifier = Modifier.fillMaxWidth(),
            placeholder = { Text("+1...", color = Neutral500) },
            singleLine = true,
            shape = RoundedCornerShape(RadiusMd),
            colors = fieldColors()
        )
        Spacer(Modifier.height(6.dp))
        Text("Operator", style = MaterialTheme.typography.labelSmall, color = Neutral500)
        Spacer(Modifier.height(2.dp))
        val opOptions = listOf("") + operators
        FormDropdown(
            selectedLabel = ls.operator.value.ifBlank { "— none —" },
            options = opOptions,
            optionLabel = { it.ifBlank { "— none —" } },
            isSelected = { it == ls.operator.value },
            onSelect = { ls.operator.value = it }
        )
    }
}

// =============================================================================
// Setup wizard dialog
// =============================================================================

@Composable
private fun SetupWizardDialog(
    gateway: Gateway,
    viewModel: TelephonyViewModel,
    context: Context,
    onClose: () -> Unit
) {
    val validateResult by viewModel.validateResult.collectAsState()
    val configPreview by viewModel.configPreview.collectAsState()
    val applyResult by viewModel.applyResult.collectAsState()
    val busy by viewModel.wizardBusy.collectAsState()

    var step by remember { mutableStateOf(0) } // 0=Validate, 1=Configure, 2=Done
    val steps = listOf("Validate", "Configure", "Done")

    DialogScaffold(
        title = "Setup · ${gateway.name}",
        onDismiss = onClose,
        confirmLabel = if (step == steps.lastIndex) "Close" else "Next",
        confirmEnabled = true,
        onConfirm = {
            if (step == steps.lastIndex) onClose() else step++
        },
        showBack = step > 0,
        onBack = { if (step > 0) step-- }
    ) {
        // Stepper
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            steps.forEachIndexed { i, label ->
                val active = i == step
                val done = i < step
                Box(
                    Modifier.weight(1f).clip(RoundedCornerShape(RadiusMd))
                        .background(
                            when {
                                active -> BbxAccent.copy(alpha = 0.2f)
                                done -> SolidGreen.copy(alpha = 0.15f)
                                else -> Neutral200
                            }
                        ).padding(vertical = 6.dp),
                    contentAlignment = Alignment.Center
                ) {
                    Text(
                        "${i + 1}. $label",
                        style = MaterialTheme.typography.labelSmall,
                        color = when {
                            active -> BbxAccent
                            done -> SolidGreen
                            else -> Neutral500
                        }
                    )
                }
            }
        }
        Spacer(Modifier.height(12.dp))

        when (step) {
            0 -> WizardValidate(gateway, validateResult, busy) { viewModel.validate(gateway.id) }
            1 -> WizardConfigure(
                gateway = gateway,
                preview = configPreview,
                applyResult = applyResult,
                busy = busy,
                onLoad = { viewModel.configPreview(gateway.id) },
                onApply = { viewModel.apply(gateway.id) },
                onCopy = { text, label -> copyToClipboard(context, text, label) }
            )
            2 -> WizardDone(gateway, validateResult != null, configPreview != null, applyResult)
        }
    }
}

@Composable
private fun WizardValidate(
    gateway: Gateway,
    result: ValidateResult?,
    busy: Boolean,
    onValidate: () -> Unit
) {
    if (result == null) {
        Text(
            "Run a quick check of reachability, AMI authentication, the SIP trunk, and each SIM.",
            style = MaterialTheme.typography.bodySmall, color = BbxDim
        )
        Spacer(Modifier.height(12.dp))
        WizardButton(if (busy) "Validating…" else "Run validation", enabled = !busy, onClick = onValidate)
    } else {
        CheckRow("Reachable", result.reachable)
        CheckRow("AMI authentication", result.amiAuth)
        CheckRow("SIP trunk online", result.trunkOnline)
        Spacer(Modifier.height(8.dp))
        Text("SIMs", style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.SemiBold), color = Neutral500)
        if (result.spans.isEmpty()) {
            Text("No SIM spans reported.", style = MaterialTheme.typography.labelSmall, color = Neutral500)
        } else {
            result.spans.forEach { s ->
                val sig = s.signal?.let { "$it%" } ?: "--"
                val phone = if (s.phoneNumber.isNotBlank()) " · ${s.phoneNumber}" else ""
                CheckRow("Span ${s.span} — ${s.carrier.ifBlank { "Unknown" }} · signal $sig$phone", s.registered)
            }
        }
        Spacer(Modifier.height(12.dp))
        WizardButton(if (busy) "Validating…" else "Re-validate", enabled = !busy, onClick = onValidate)
    }
}

@Composable
private fun WizardConfigure(
    gateway: Gateway,
    preview: ConfigPreview?,
    applyResult: ApplyResult?,
    busy: Boolean,
    onLoad: () -> Unit,
    onApply: () -> Unit,
    onCopy: (String, String) -> Unit
) {
    if (preview == null) {
        Text(
            "Load the gateway-side steps and the BlackBox-side Asterisk configuration.",
            style = MaterialTheme.typography.bodySmall, color = BbxDim
        )
        Spacer(Modifier.height(12.dp))
        WizardButton(if (busy) "Loading…" else "Load configuration", enabled = !busy, onClick = onLoad)
        return
    }

    Text(
        "Gateway-side steps (NeoGate GUI)",
        style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.SemiBold),
        color = BbxWhite
    )
    Spacer(Modifier.height(6.dp))
    if (preview.tgSteps.isEmpty()) {
        Text("No gateway-side steps provided.", style = MaterialTheme.typography.labelSmall, color = Neutral500)
    } else {
        preview.tgSteps.forEachIndexed { i, stepText ->
            Row(
                Modifier.fillMaxWidth().padding(vertical = 3.dp),
                verticalAlignment = Alignment.Top
            ) {
                Text(
                    "${i + 1}.",
                    style = MaterialTheme.typography.bodySmall.copy(fontWeight = FontWeight.Bold),
                    color = BbxAccent,
                    modifier = Modifier.width(20.dp)
                )
                Text(stepText, style = MaterialTheme.typography.bodySmall, color = BbxDim, modifier = Modifier.weight(1f))
                Spacer(Modifier.width(6.dp))
                CardActionButton("Copy", BbxAccent) { onCopy(stepText, "Step ${i + 1}") }
            }
        }
    }

    Spacer(Modifier.height(12.dp))
    Row(verticalAlignment = Alignment.CenterVertically) {
        Text(
            "BlackBox-side Asterisk config",
            style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.SemiBold),
            color = BbxWhite,
            modifier = Modifier.weight(1f)
        )
        CardActionButton("Copy", BbxAccent) { onCopy(preview.asteriskConf, "Asterisk config") }
    }
    Spacer(Modifier.height(6.dp))
    Box(
        Modifier.fillMaxWidth().heightIn(max = 200.dp)
            .clip(RoundedCornerShape(RadiusMd)).background(Color(0xFF0A0A0A))
            .verticalScroll(rememberScrollState()).padding(10.dp)
    ) {
        Text(
            preview.asteriskConf,
            style = MaterialTheme.typography.bodySmall.copy(fontFamily = FontFamily.Monospace, fontSize = 11.sp),
            color = SolidGreen
        )
    }

    // Apply result notices
    when {
        applyResult?.restartRecommended == true -> {
            Spacer(Modifier.height(10.dp))
            NoticeBox("A BlackBox restart is needed to finish.", warn = true)
        }
        applyResult?.applied == true -> {
            Spacer(Modifier.height(10.dp))
            NoticeBox(
                "Configuration applied" + (if (applyResult.reloadOk) " and reloaded." else "."),
                warn = false
            )
        }
    }

    Spacer(Modifier.height(12.dp))
    WizardButton(if (busy) "Applying…" else "Apply our-side config", enabled = !busy, onClick = onApply)
}

@Composable
private fun WizardDone(
    gateway: Gateway,
    validated: Boolean,
    reviewed: Boolean,
    applyResult: ApplyResult?
) {
    Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally) {
        Text("✓", fontSize = 40.sp, color = SolidGreen)
        Spacer(Modifier.height(8.dp))
        Text(
            "Setup complete",
            style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.SemiBold),
            color = BbxWhite
        )
        Spacer(Modifier.height(4.dp))
        Text(
            "${gateway.name} has been walked through setup.",
            style = MaterialTheme.typography.bodySmall, color = BbxDim
        )
    }
    Spacer(Modifier.height(12.dp))
    SummaryRow(if (validated) "Connectivity validated" else "Validation skipped", validated)
    SummaryRow(if (reviewed) "Configuration reviewed" else "Configuration not loaded", reviewed)
    SummaryRow(
        if (applyResult?.applied == true) "Our-side config applied" else "Our-side config not applied",
        applyResult?.applied == true
    )
    if (applyResult?.restartRecommended == true) {
        Spacer(Modifier.height(10.dp))
        NoticeBox("A BlackBox restart is still needed to finish.", warn = true)
    }
}

@Composable
private fun SummaryRow(label: String, ok: Boolean) {
    Row(Modifier.fillMaxWidth().padding(vertical = 2.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(if (ok) "✓" else "·", color = if (ok) SolidGreen else Neutral500, modifier = Modifier.width(20.dp))
        Text(label, style = MaterialTheme.typography.bodySmall, color = BbxDim)
    }
}

@Composable
private fun CheckRow(label: String, ok: Boolean) {
    Row(Modifier.fillMaxWidth().padding(vertical = 3.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(
            if (ok) "✓" else "✗",
            color = if (ok) SolidGreen else BbxAccent,
            fontWeight = FontWeight.Bold,
            modifier = Modifier.width(20.dp)
        )
        Text(label, style = MaterialTheme.typography.bodySmall, color = BbxDim)
    }
}

@Composable
private fun NoticeBox(text: String, warn: Boolean) {
    val color = if (warn) Color(0xFFFFA726) else SolidGreen
    Box(
        Modifier.fillMaxWidth().clip(RoundedCornerShape(RadiusMd))
            .background(color.copy(alpha = 0.12f))
            .border(1.dp, color.copy(alpha = 0.35f), RoundedCornerShape(RadiusMd))
            .padding(10.dp)
    ) {
        Text(text, style = MaterialTheme.typography.bodySmall, color = color)
    }
}

@Composable
private fun WizardButton(label: String, enabled: Boolean, onClick: () -> Unit) {
    val feedback = rememberPressFeedback()
    Button(
        onClick = { feedback(); onClick() },
        enabled = enabled,
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(RadiusMd),
        colors = ButtonDefaults.buttonColors(
            containerColor = BbxAccent,
            contentColor = BbxWhite,
            disabledContainerColor = Neutral200
        )
    ) {
        Text(label)
    }
}

// =============================================================================
// Confirm remove dialog
// =============================================================================

@Composable
private fun ConfirmRemoveDialog(
    gatewayName: String,
    onConfirm: () -> Unit,
    onDismiss: () -> Unit
) {
    DialogScaffold(
        title = "Remove Gateway",
        onDismiss = onDismiss,
        confirmLabel = "Remove",
        confirmEnabled = true,
        confirmColor = BbxAccent,
        onConfirm = onConfirm
    ) {
        Text(
            "Remove \"$gatewayName\"? This will disconnect all associated SIM channels.",
            style = MaterialTheme.typography.bodyMedium, color = BbxDim
        )
    }
}

// =============================================================================
// Shared dialog scaffold + form primitives
// =============================================================================

@Composable
private fun DialogScaffold(
    title: String,
    onDismiss: () -> Unit,
    confirmLabel: String,
    confirmEnabled: Boolean,
    onConfirm: () -> Unit,
    confirmColor: Color = SolidGreen,
    showBack: Boolean = false,
    onBack: () -> Unit = {},
    content: @Composable () -> Unit
) {
    val feedback = rememberPressFeedback()
    androidx.compose.ui.window.Dialog(onDismissRequest = onDismiss) {
        GlassCard(
            modifier = Modifier.fillMaxWidth().padding(8.dp),
            bg = Neutral100
        ) {
            Column(Modifier.fillMaxWidth().padding(16.dp)) {
                Text(
                    title,
                    style = MaterialTheme.typography.titleLarge.copy(fontWeight = FontWeight.Bold),
                    color = BbxWhite
                )
                Spacer(Modifier.height(12.dp))
                Column(
                    Modifier.fillMaxWidth().heightIn(max = 460.dp).verticalScroll(rememberScrollState())
                ) {
                    content()
                }
                Spacer(Modifier.height(16.dp))
                Row(
                    Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    if (showBack) {
                        TextButton(onClick = { feedback(); onBack() }) { Text("Back", color = BbxDim) }
                    }
                    Spacer(Modifier.weight(1f))
                    TextButton(onClick = { feedback(); onDismiss() }) { Text("Cancel", color = BbxDim) }
                    Button(
                        onClick = { feedback(); onConfirm() },
                        enabled = confirmEnabled,
                        shape = RoundedCornerShape(RadiusMd),
                        colors = ButtonDefaults.buttonColors(
                            containerColor = confirmColor,
                            contentColor = BbxWhite,
                            disabledContainerColor = Neutral200
                        )
                    ) { Text(confirmLabel) }
                }
            }
        }
    }
}

@Composable
private fun SectionLabel(text: String) {
    Spacer(Modifier.height(12.dp))
    Text(
        text,
        style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.SemiBold),
        color = BbxAccent
    )
    Spacer(Modifier.height(4.dp))
}

@Composable
private fun fieldColors() = OutlinedTextFieldDefaults.colors(
    focusedBorderColor = BbxAccent,
    unfocusedBorderColor = Neutral200,
    cursorColor = BbxAccent,
    focusedTextColor = BbxWhite,
    unfocusedTextColor = BbxWhite
)

@Composable
private fun FormField(
    label: String,
    value: String,
    onValueChange: (String) -> Unit,
    placeholder: String,
    password: Boolean = false,
    number: Boolean = false
) {
    Spacer(Modifier.height(8.dp))
    Text(label, style = MaterialTheme.typography.labelMedium, color = Neutral500)
    Spacer(Modifier.height(4.dp))
    OutlinedTextField(
        value = value,
        onValueChange = onValueChange,
        modifier = Modifier.fillMaxWidth(),
        placeholder = { Text(placeholder, color = Neutral500) },
        singleLine = true,
        shape = RoundedCornerShape(RadiusMd),
        visualTransformation = if (password) PasswordVisualTransformation() else androidx.compose.ui.text.input.VisualTransformation.None,
        keyboardOptions = if (number)
            androidx.compose.foundation.text.KeyboardOptions(keyboardType = KeyboardType.Number)
        else androidx.compose.foundation.text.KeyboardOptions.Default,
        colors = fieldColors()
    )
}

@Composable
private fun <T> FormDropdown(
    selectedLabel: String,
    options: List<T>,
    optionLabel: (T) -> String,
    isSelected: (T) -> Boolean,
    onSelect: (T) -> Unit
) {
    var showMenu by remember { mutableStateOf(false) }
    val feedback = rememberPressFeedback()
    Box(Modifier.fillMaxWidth()) {
        Button(
            onClick = { feedback(); showMenu = true },
            modifier = Modifier.fillMaxWidth().height(44.dp),
            shape = RoundedCornerShape(RadiusMd),
            contentPadding = PaddingValues(horizontal = 12.dp),
            colors = ButtonDefaults.buttonColors(containerColor = Neutral200, contentColor = BbxWhite)
        ) {
            Text(selectedLabel, modifier = Modifier.weight(1f), fontSize = 13.sp)
            Text(" ▾", color = BbxDim, fontSize = 12.sp)
        }
        DropdownMenu(expanded = showMenu, onDismissRequest = { showMenu = false }) {
            options.forEach { opt ->
                val sel = isSelected(opt)
                DropdownMenuItem(
                    text = {
                        Text(
                            optionLabel(opt),
                            color = if (sel) BbxAccent else BbxWhite,
                            fontWeight = if (sel) FontWeight.Bold else FontWeight.Normal
                        )
                    },
                    onClick = {
                        feedback()
                        showMenu = false
                        onSelect(opt)
                    }
                )
            }
        }
    }
}
