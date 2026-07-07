package com.aiblackbox.portal.ui.devices

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.MeshDevice
import com.aiblackbox.portal.data.model.parseMeshDevices
import com.aiblackbox.portal.data.model.parseOperators
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.net.URLEncoder

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

    private val _devices = MutableStateFlow<List<MeshDevice>>(emptyList())
    val devices: StateFlow<List<MeshDevice>> = _devices.asStateFlow()

    private val _operators = MutableStateFlow<List<String>>(emptyList())
    val operators: StateFlow<List<String>> = _operators.asStateFlow()

    private val _isLoading = MutableStateFlow(false)
    val isLoading: StateFlow<Boolean> = _isLoading.asStateFlow()

    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error.asStateFlow()

    /** Non-fatal hint surfaced when the operator roster fails to load (so the empty
     *  owner dropdown isn't silently unexplained). Separate from [_error] (device load)
     *  so a roster hiccup never blanks the device list. */
    private val _operatorHint = MutableStateFlow<String?>(null)
    val operatorHint: StateFlow<String?> = _operatorHint.asStateFlow()

    private val _actionMessage = MutableStateFlow<String?>(null)
    val actionMessage: StateFlow<String?> = _actionMessage.asStateFlow()

    /** True once the first mesh load has completed (so the UI can distinguish
     *  "loading" from a genuinely empty tailnet). */
    private val _loadedOnce = MutableStateFlow(false)
    val loadedOnce: StateFlow<Boolean> = _loadedOnce.asStateFlow()

    /** Optional operator scope for the mesh fetch. When set, `refresh()` fetches
     *  `/devices/mesh?operator=X` (backend returns rows where owner==null OR owner==X);
     *  null = whole tailnet. This is SCOPE, not declutter — the client-side
     *  [_hideUnassigned] filter is what actually hides unclaimed nodes. */
    private val _filter = MutableStateFlow<String?>(null)
    val filter: StateFlow<String?> = _filter.asStateFlow()

    /** Client-side view filter: when true the UI renders only claimed (owned) devices.
     *  Default OFF so a fresh box's unclaimed — hence claimable — nodes stay visible. */
    private val _hideUnassigned = MutableStateFlow(false)
    val hideUnassigned: StateFlow<Boolean> = _hideUnassigned.asStateFlow()

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
                val scope = _filter.value
                val path = if (scope.isNullOrBlank()) "/devices/mesh"
                    else "/devices/mesh?operator=" + URLEncoder.encode(scope, "UTF-8")
                val raw = api.get(path)
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

    /** Scope the mesh fetch to a single operator (null = whole tailnet), then refetch. */
    fun setFilter(operator: String?) {
        val next = operator?.takeIf { it.isNotBlank() }
        if (_filter.value == next) return
        _filter.value = next
        refresh()
    }

    /** Toggle the client-side hide-unassigned view filter (applied in the UI, so no
     *  refetch is needed). */
    fun setHideUnassigned(hide: Boolean) {
        _hideUnassigned.value = hide
    }

    private fun loadOperators() {
        val api = api ?: return
        viewModelScope.launch {
            try {
                val ops = parseOperators(api.get("/operators"))
                _operators.value = ops
                _operatorHint.value = if (ops.isEmpty()) "No operators configured yet." else null
            } catch (e: Exception) {
                // Keep any operators already known; surface a non-fatal hint so the
                // (possibly empty) owner dropdown isn't silently unexplained.
                _operatorHint.value = "Couldn't load operators: ${e.message}"
            }
        }
    }

    /** Claim an UNOWNED device for [operator] (assigning auto-registers a tailnet node).
     *  For an owned device use [rehome] (confirm-guarded) instead. */
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

    /**
     * Re-home an owned device to [newOperator] in ONE coroutine: unassign, then re-claim.
     * A single coroutine (not two independent fire-and-forget calls) so the two POSTs
     * cannot interleave with another action. The unassign requester is the TARGET
     * [newOperator] (guaranteed a live-roster entry, so the backend's live-operator check
     * passes even when [currentOwner] is a phantom off-roster owner) — mirrors the Portal
     * re-home. [currentOwner] is passed for the caller's confirmation copy. Failures
     * surface as a PERSISTENT inline [_error], not just a toast.
     */
    fun rehome(deviceId: String, newOperator: String, currentOwner: String) = viewModelScope.launch {
        val api = api ?: return@launch
        try {
            api.post("/devices/$deviceId/unassign", buildJsonObject { put("operator", newOperator) }.toString())
            api.post("/devices/$deviceId/operator", buildJsonObject { put("operator", newOperator) }.toString())
            _actionMessage.value = "Reassigned to $newOperator"
            refresh()
        } catch (e: Exception) {
            _error.value = "Reassign failed: ${e.message}"
            refresh() // re-render true backend state so a partial re-home isn't left asserting a stale owner
        }
    }

    /**
     * Clear a device's owner (and primary flag) so it becomes claimable again. [requester]
     * is a live operator for provenance/logging (the caller supplies the current owner if
     * it's on the live roster, else any live operator as a phantom-owner fallback). Failures
     * surface as a PERSISTENT inline [_error].
     */
    fun unassign(deviceId: String, requester: String) = viewModelScope.launch {
        val api = api ?: return@launch
        try {
            api.post("/devices/$deviceId/unassign", buildJsonObject { put("operator", requester) }.toString())
            _actionMessage.value = "Unassigned"
            refresh()
        } catch (e: Exception) {
            _error.value = "Unassign failed: ${e.message}"
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
