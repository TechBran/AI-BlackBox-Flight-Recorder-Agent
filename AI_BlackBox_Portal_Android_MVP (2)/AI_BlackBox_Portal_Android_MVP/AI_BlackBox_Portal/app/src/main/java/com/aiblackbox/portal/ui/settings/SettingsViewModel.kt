package com.aiblackbox.portal.ui.settings

import android.app.Application
import android.content.Context
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.store.BlackBoxStore
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

data class RegisteredApp(
    val appId: String = "",
    val name: String = "",
    val port: Int = 0,
    val directory: String = "",
    val operator: String = "",
    val createdAt: String = ""
)

class SettingsViewModel(application: Application) : AndroidViewModel(application) {
    val store = BlackBoxStore(application)
    internal var api: BlackBoxApi? = null

    private val _operators = MutableStateFlow(listOf("Brandon"))
    val operators: StateFlow<List<String>> = _operators.asStateFlow()

    private val _apps = MutableStateFlow<List<RegisteredApp>>(emptyList())
    val apps: StateFlow<List<RegisteredApp>> = _apps.asStateFlow()

    // Voice catalog — defaults to the full offline fallback so the picker is never
    // empty; loadVoiceCatalog() swaps in the live /tts/catalog when reachable.
    private val _voiceGroups = MutableStateFlow(com.aiblackbox.portal.data.repository.TTS_VOICE_GROUPS)
    val voiceGroups: StateFlow<List<com.aiblackbox.portal.data.repository.VoiceGroup>> = _voiceGroups.asStateFlow()

    fun loadVoiceCatalog() {
        val api = api ?: return
        viewModelScope.launch {
            try {
                _voiceGroups.value = com.aiblackbox.portal.data.repository.TtsRepository(api).fetchCatalog()
            } catch (_: Exception) { /* keep the offline default already in _voiceGroups */ }
        }
    }

    fun initialize(origin: String) {
        if (origin.isBlank() || api != null) return
        api = BlackBoxApi(origin)
        loadOperators()
        loadApps()
    }

    fun setProvider(provider: String) {
        viewModelScope.launch { store.setProvider(provider) }
    }

    fun setModel(model: String, provider: String) {
        viewModelScope.launch {
            store.setModel(model)
            store.setString("model_$provider", model)
        }
    }

    fun setOperator(operator: String) {
        viewModelScope.launch { store.setOperator(operator) }
    }

    fun setStreamingEnabled(enabled: Boolean) {
        viewModelScope.launch { store.setStreamingEnabled(enabled) }
    }

    fun setOperatorVoice(operator: String, voiceValue: String) {
        viewModelScope.launch { store.setOperatorVoice(operator, voiceValue) }
    }

    fun addOperator(name: String) {
        val api = api ?: return
        viewModelScope.launch {
            try {
                val body = """{"name":"$name"}"""
                api.post("/operator/add", body)
                store.setOperator(name)
            } catch (_: Exception) {}
        }
    }

    fun setUseNative(context: Context, useNative: Boolean) {
        context.getSharedPreferences("bbx_prefs", Context.MODE_PRIVATE)
            .edit().putBoolean("use_native_ui", useNative).apply()
    }

    /** Trigger a conversation checkpoint (POST /checkpoint) */
    fun triggerCheckpoint() {
        val api = api ?: return
        viewModelScope.launch {
            try {
                api.post("/checkpoint", "{}")
            } catch (_: Exception) {}
        }
    }

    /** Disconnect Gmail for an operator */
    fun disconnectGmail(operator: String) {
        val api = api ?: return
        viewModelScope.launch {
            try {
                api.post("/gmail/disconnect/$operator", "{}")
            } catch (_: Exception) {}
        }
    }

    /** Cancel all stuck tasks (POST /tasks/cancel-all) */
    fun cancelAllTasks() {
        val api = api ?: return
        viewModelScope.launch {
            try {
                api.post("/tasks/cancel-all", "{}")
            } catch (_: Exception) {}
        }
    }

    /** Fetch pairing token from backend (POST /pair/start) — matches Portal flow */
    suspend fun fetchPairToken(): Pair<String, Long>? {
        return try {
            val response = api?.post("/pair/start", "{}") ?: return null
            val json = Json { ignoreUnknownKeys = true }
            val obj = json.parseToJsonElement(response).jsonObject
            val token = obj["token"]?.jsonPrimitive?.content ?: return null
            val exp = obj["exp"]?.jsonPrimitive?.content?.toLongOrNull() ?: 0L
            Pair(token, exp)
        } catch (_: Exception) {
            null
        }
    }

    /** Restart the BlackBox service (POST /restart) */
    fun restartService() {
        val api = api ?: return
        viewModelScope.launch {
            try {
                api.post("/restart", "{}")
            } catch (_: Exception) {}
        }
    }

    private fun loadApps() {
        viewModelScope.launch {
            try {
                val response = api?.get("/agent/apps") ?: return@launch
                val json = Json { ignoreUnknownKeys = true }
                val obj = json.parseToJsonElement(response).jsonObject
                val appsList = obj["apps"]?.jsonArray?.mapNotNull { elem ->
                    val a = elem.jsonObject
                    RegisteredApp(
                        appId = a["app_id"]?.jsonPrimitive?.content ?: "",
                        name = a["name"]?.jsonPrimitive?.content ?: "Unnamed",
                        port = a["port"]?.jsonPrimitive?.content?.toIntOrNull() ?: 0,
                        directory = a["directory"]?.jsonPrimitive?.content ?: "",
                        operator = a["operator"]?.jsonPrimitive?.content ?: "",
                        createdAt = a["created_at"]?.jsonPrimitive?.content ?: ""
                    )
                } ?: emptyList()
                _apps.value = appsList
            } catch (_: Exception) {}
        }
    }

    private fun loadOperators() {
        viewModelScope.launch {
            try {
                val response = api?.get("/operators") ?: return@launch
                val json = Json { ignoreUnknownKeys = true }
                val obj = json.parseToJsonElement(response).jsonObject
                val ops = obj["operators"]?.jsonArray?.mapNotNull { elem ->
                    elem.jsonObject["operator"]?.jsonPrimitive?.content
                } ?: emptyList()
                if (ops.isNotEmpty()) _operators.value = ops
            } catch (_: Exception) {
                // Keep default operator list on failure
            }
        }
    }
}
