package com.aiblackbox.portal.ui.devices

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.MeshDevice
import com.aiblackbox.portal.data.model.parseMeshDevices
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put

/**
 * Backs the System-Menu "Devices" (tailnet mesh) view — M3 task 3.8. Consumes the
 * same `/devices` API the Portal Devices view uses:
 *   - `GET  /devices/mesh`                       → whole tailnet + ownership annotations
 *   - `POST /devices/{id}/operator`              → claim/reassign a device's owner
 *   - `POST /devices/{id}/primary`               → set the owner's primary device
 *   - `POST /devices/{id}/default-provider`      → set/clear the device's frontier provider
 * plus `GET /operators` for the owner picker. All mutations refresh the mesh so the UI
 * reflects the backend's post-mutation truth (e.g. a reassign clearing the primary flag).
 */
class MeshDeviceViewModel(application: Application) : AndroidViewModel(application) {
    private var api: BlackBoxApi? = null
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    private val _devices = MutableStateFlow<List<MeshDevice>>(emptyList())
    val devices: StateFlow<List<MeshDevice>> = _devices.asStateFlow()

    private val _operators = MutableStateFlow<List<String>>(emptyList())
    val operators: StateFlow<List<String>> = _operators.asStateFlow()

    private val _isLoading = MutableStateFlow(false)
    val isLoading: StateFlow<Boolean> = _isLoading.asStateFlow()

    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error.asStateFlow()

    private val _actionMessage = MutableStateFlow<String?>(null)
    val actionMessage: StateFlow<String?> = _actionMessage.asStateFlow()

    /** True once the first mesh load has completed (so the UI can distinguish
     *  "loading" from a genuinely empty tailnet). */
    private val _loadedOnce = MutableStateFlow(false)
    val loadedOnce: StateFlow<Boolean> = _loadedOnce.asStateFlow()

    fun initialize(origin: String) {
        if (origin.isBlank() || api != null) return
        api = BlackBoxApi(origin)
        loadOperators()
        refresh()
    }

    fun refresh() {
        val api = api ?: return
        _isLoading.value = true
        viewModelScope.launch {
            try {
                val raw = api.get("/devices/mesh")
                _devices.value = parseMeshDevices(raw)
                _error.value = null
            } catch (e: Exception) {
                _error.value = "Failed to load devices: ${e.message}"
                _devices.value = emptyList()
            } finally {
                _isLoading.value = false
                _loadedOnce.value = true
            }
        }
    }

    private fun loadOperators() {
        val api = api ?: return
        viewModelScope.launch {
            try {
                val raw = api.get("/operators")
                val ops = json.parseToJsonElement(raw).jsonObject["operators"]
                    ?.jsonArray?.mapNotNull { it.jsonObject["operator"]?.jsonPrimitive?.content }
                    ?: emptyList()
                if (ops.isNotEmpty()) _operators.value = ops
            } catch (_: Exception) {
                // Owner picker degrades to whatever operators are already known.
            }
        }
    }

    /** Claim (or reassign) a device to [operator]. Reassign clears its primary flag
     *  server-side, which the follow-up refresh reflects. */
    fun assignOperator(deviceId: String, operator: String) {
        val api = api ?: return
        viewModelScope.launch {
            try {
                val body = buildJsonObject { put("operator", operator) }.toString()
                api.post("/devices/$deviceId/operator", body)
                _actionMessage.value = "Assigned to $operator"
                refresh()
            } catch (e: Exception) {
                _actionMessage.value = "Assign failed: ${e.message}"
            }
        }
    }

    /** Make [deviceId] the primary device for its owner [operator]. The backend
     *  requires the device be owned by that operator (operator isolation). */
    fun setPrimary(deviceId: String, operator: String) {
        val api = api ?: return
        viewModelScope.launch {
            try {
                val body = buildJsonObject { put("operator", operator) }.toString()
                api.post("/devices/$deviceId/primary", body)
                _actionMessage.value = "Primary set"
                refresh()
            } catch (e: Exception) {
                _actionMessage.value = "Set primary failed: ${e.message}"
            }
        }
    }

    /** Set (or clear, when [provider] is null) the device's default frontier provider.
     *  Passes [owner] when present so the backend enforces ownership. */
    fun setDefaultProvider(deviceId: String, provider: String?, owner: String?) {
        val api = api ?: return
        viewModelScope.launch {
            try {
                val body = buildJsonObject {
                    put("provider", if (provider == null) JsonNull else JsonPrimitive(provider))
                    if (!owner.isNullOrBlank()) put("operator", owner)
                }.toString()
                api.post("/devices/$deviceId/default-provider", body)
                _actionMessage.value = if (provider == null) "Provider cleared" else "Provider → $provider"
                refresh()
            } catch (e: Exception) {
                _actionMessage.value = "Set provider failed: ${e.message}"
            }
        }
    }

    fun clearActionMessage() { _actionMessage.value = null }
}
