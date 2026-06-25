package com.aiblackbox.portal.ui.settings

import android.app.Application
import android.os.Build
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.remote.TailnetAddress
import com.aiblackbox.portal.data.store.NotificationSubscriptionStore
import com.aiblackbox.portal.util.DeviceId
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * POST /notifications/subscribe body. Built with [Json.encodeToString] — NEVER string
 * interpolation (operator names and the device model are user/OEM data that could break
 * a hand-built JSON string). [tailnetName] is null when the phone is not on the tailnet;
 * the backend stores it and the bus resolver joins it against the live online set, so a
 * null simply means "not currently deliverable" (the row is still recorded). [operators]
 * carries the named operators OR the single sentinel "all".
 */
@Serializable
data class SubscribeBody(
    val device_id: String,
    val tailnet_name: String?,
    val device_kind: String = "android",
    val display_name: String,
    val operators: List<String>,
)

/**
 * Owns the phone's notification SUBSCRIPTION — the delivery-critical half of MN.6.
 *
 * Modeled on the per-operator persona panel ([SettingsViewModel]'s persona section): an
 * api-backed load/save VM whose UI binds to the live [com.aiblackbox.portal.ui.chat.ChatViewModel]
 * operator flow (passed in from the sheet, auto-updating — no manual refresh).
 *
 * **Write-through to BOTH sides (routing + local re-check stay in sync).** Every change
 * writes (a) the backend via `POST /notifications/subscribe` (the bus's routing record:
 * `tailnet_name` join key + the operator set) AND (b) the device-local
 * [NotificationSubscriptionStore] (the second-layer allow-list the inbound `/notify`
 * authorize re-check reads). Mapping:
 *   - "All operators" ON  → backend operators=["all"], local store CLEARED (empty == accept-all).
 *   - specific operators  → backend operators=[those], local store = EXACTLY those.
 *   - nothing selected    → backend operators=[] (no delivery), local store cleared.
 *
 * **Opt-in default.** Nothing is selected until the user picks. On open we GET the backend
 * row (authoritative) and reconcile the UI + local store to it.
 *
 * **Tailnet.** [tailnetAvailable] reflects whether the phone has a 100.64.0.0/10 address;
 * when false the UI still lets the user subscribe (local + backend) but hints that delivery
 * needs Tailscale — the backend records the row and starts delivering once the phone is online.
 */
class NotificationSubscriptionViewModel(application: Application) : AndroidViewModel(application) {

    private val store = NotificationSubscriptionStore(application)
    private var api: BlackBoxApi? = null
    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }

    private val _allSelected = MutableStateFlow(false)
    val allSelected: StateFlow<Boolean> = _allSelected.asStateFlow()

    /** The currently-subscribed named operators (ignored while [allSelected] is true). */
    private val _selectedOperators = MutableStateFlow<Set<String>>(emptySet())
    val selectedOperators: StateFlow<Set<String>> = _selectedOperators.asStateFlow()

    /** Whether the phone currently has a tailnet (100.64.0.0/10) address. */
    private val _tailnetAvailable = MutableStateFlow(false)
    val tailnetAvailable: StateFlow<Boolean> = _tailnetAvailable.asStateFlow()

    private val deviceId: String get() = DeviceId.stable(getApplication())

    /** Initialize the api (idempotent) + reconcile from the backend. Call on sheet open. */
    fun initialize(origin: String) {
        if (origin.isBlank()) return
        if (api == null) api = BlackBoxApi(origin)
        _tailnetAvailable.value = TailnetAddress.localTailnetIpv4() != null
        reconcileFromBackend()
    }

    /**
     * GET the device's backend subscription row and adopt it as the source of truth
     * (404 → no subscription yet → opt-in default of nothing). Also mirror it into the
     * device-local store so the /notify re-check matches what the bus will route.
     */
    private fun reconcileFromBackend() {
        val a = api ?: return
        viewModelScope.launch {
            try {
                val resp = a.get("/notifications/subscriptions?device_id=" + android.net.Uri.encode(deviceId))
                val obj = json.parseToJsonElement(resp).jsonObject
                val all = obj["all"]?.jsonPrimitive?.booleanOrNull ?: false
                val ops = obj["operators"]?.jsonArray
                    ?.mapNotNull { it.jsonPrimitive.content.takeIf(String::isNotBlank) }
                    ?.toSet() ?: emptySet()
                _allSelected.value = all
                _selectedOperators.value = ops
                // Keep the local /notify allow-list in lockstep with the backend record.
                if (all) store.clear() else store.setOperators(ops)
            } catch (_: Exception) {
                // 404 (never subscribed) or unreachable: keep the opt-in default. Do NOT
                // touch the local store — leave whatever was already there.
            }
        }
    }

    /** Toggle "All operators". Write-through: backend ["all"] / [] + local clear. */
    fun setAll(enabled: Boolean) {
        _allSelected.value = enabled
        if (enabled) _selectedOperators.value = emptySet()
        viewModelScope.launch {
            store.clear() // all == accept-all; none == backend won't route here anyway
            pushSubscription(all = enabled, operators = emptySet())
        }
    }

    /** Toggle a single operator. No-op visually while [allSelected] is on (UI disables rows). */
    fun toggleOperator(operator: String, enabled: Boolean) {
        val op = operator.trim()
        if (op.isEmpty()) return
        val next = if (enabled) _selectedOperators.value + op else _selectedOperators.value - op
        _selectedOperators.value = next
        _allSelected.value = false
        viewModelScope.launch {
            store.setOperators(next)
            pushSubscription(all = false, operators = next)
        }
    }

    /**
     * POST the subscription to the backend with a serializer-built body. [tailnet_name]
     * is re-read each push (the phone may have just joined/left the tailnet). On "all",
     * [operators] carries the single "all" sentinel; otherwise the named set (possibly
     * empty == unsubscribe-from-all-named, which the backend records as no delivery).
     */
    private suspend fun pushSubscription(all: Boolean, operators: Set<String>) {
        val a = api ?: return
        val tailnet = TailnetAddress.localTailnetIpv4()
        _tailnetAvailable.value = tailnet != null
        val ops = if (all) listOf("all") else operators.toList()
        try {
            val body = json.encodeToString(
                SubscribeBody(
                    device_id = deviceId,
                    tailnet_name = tailnet,
                    device_kind = "android",
                    display_name = Build.MODEL ?: "Android device",
                    operators = ops,
                )
            )
            a.post("/notifications/subscribe", body)
        } catch (e: Exception) {
            android.util.Log.w("NotifSubscribe", "subscribe push failed", e)
        }
    }
}
