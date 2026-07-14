package com.aiblackbox.portal.data.store

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map

private val Context.dataStore: DataStore<Preferences> by preferencesDataStore(name = "bbx_settings")

class BlackBoxStore(private val context: Context) {

    companion object {
        val KEY_OPERATOR = stringPreferencesKey("operator")
        val KEY_PROVIDER = stringPreferencesKey("provider")
        val KEY_CLI_AGENT_PROVIDER = stringPreferencesKey("cli_agent_provider")
        val KEY_MODEL = stringPreferencesKey("model")
        val KEY_STREAMING = booleanPreferencesKey("streaming_enabled")
        val KEY_CLAUDE_MODEL = stringPreferencesKey("claude_model")
        val KEY_TTS_VOICE = stringPreferencesKey("tts_voice")
        val KEY_ORIGIN = stringPreferencesKey("origin")
        val KEY_EMBER_MODE = stringPreferencesKey("ember_mode")
        val KEY_PARTICLE_MODE = stringPreferencesKey("particle_mode")
    }

    // Origin (server URL)
    val origin: Flow<String> = context.dataStore.data.map { it[KEY_ORIGIN] ?: "" }
    suspend fun setOrigin(value: String) { context.dataStore.edit { it[KEY_ORIGIN] = value } }

    // Operator
    val operator: Flow<String> = context.dataStore.data.map { it[KEY_OPERATOR] ?: "Brandon" }
    suspend fun setOperator(value: String) { context.dataStore.edit { it[KEY_OPERATOR] = value } }

    // Provider
    val provider: Flow<String> = context.dataStore.data.map { it[KEY_PROVIDER] ?: "gemini" }
    suspend fun setProvider(value: String) { context.dataStore.edit { it[KEY_PROVIDER] = value } }

    // Ember Backdrop VISIBILITY mode — "off" / "generating" / "always" (default "always")
    val emberMode: Flow<String> = context.dataStore.data.map { it[KEY_EMBER_MODE] ?: "always" }
    suspend fun setEmberMode(value: String) { context.dataStore.edit { it[KEY_EMBER_MODE] = value } }

    // Particle FIELD style — "stars" / "embers" / "matrix" (default "stars").
    // Orthogonal to emberMode (which governs OFF/GENERATING/ALWAYS visibility).
    val particleMode: Flow<String> = context.dataStore.data.map { it[KEY_PARTICLE_MODE] ?: "stars" }
    suspend fun setParticleMode(value: String) { context.dataStore.edit { it[KEY_PARTICLE_MODE] = value } }

    // CLI Agent Provider
    val cliAgentProviderFlow: Flow<String> =
        context.dataStore.data.map { it[KEY_CLI_AGENT_PROVIDER] ?: "claude" }
    suspend fun setCliAgentProvider(value: String) {
        context.dataStore.edit { it[KEY_CLI_AGENT_PROVIDER] = value }
    }

    // Model
    val model: Flow<String> = context.dataStore.data.map { it[KEY_MODEL] ?: "" }
    suspend fun setModel(value: String) { context.dataStore.edit { it[KEY_MODEL] = value } }

    // Streaming
    val streamingEnabled: Flow<Boolean> = context.dataStore.data.map { it[KEY_STREAMING] ?: true }
    suspend fun setStreamingEnabled(value: Boolean) { context.dataStore.edit { it[KEY_STREAMING] = value } }

    // Claude Model
    val claudeModel: Flow<String> = context.dataStore.data.map { it[KEY_CLAUDE_MODEL] ?: "sonnet" }
    suspend fun setClaudeModel(value: String) { context.dataStore.edit { it[KEY_CLAUDE_MODEL] = value } }

    // TTS Voice (global default)
    val ttsVoice: Flow<String> = context.dataStore.data.map { it[KEY_TTS_VOICE] ?: "openai:onyx" }
    suspend fun setTtsVoice(value: String) { context.dataStore.edit { it[KEY_TTS_VOICE] = value } }

    // Per-operator TTS voice — matches Portal OPERATOR_VOICE_STORE pattern
    // Key format: "voice_{operator}" → "provider:voice" (e.g., "openai:alloy")
    fun getOperatorVoice(operator: String): Flow<String> =
        context.dataStore.data.map { it[stringPreferencesKey("voice_$operator")] ?: "openai:onyx" }

    suspend fun setOperatorVoice(operator: String, voiceValue: String) {
        context.dataStore.edit {
            it[stringPreferencesKey("voice_$operator")] = voiceValue
            // Also update global default
            it[KEY_TTS_VOICE] = voiceValue
        }
    }

    // Auto-TTS toggle — matches Portal localStorage bb_auto_tts
    val KEY_AUTO_TTS = booleanPreferencesKey("auto_tts")
    val autoTtsEnabled: Flow<Boolean> = context.dataStore.data.map { it[KEY_AUTO_TTS] ?: false }
    suspend fun setAutoTtsEnabled(value: Boolean) { context.dataStore.edit { it[KEY_AUTO_TTS] = value } }

    // Generic string get/set for operator-scoped preferences
    suspend fun setString(key: String, value: String) {
        context.dataStore.edit { it[stringPreferencesKey(key)] = value }
    }
    fun getString(key: String, default: String = ""): Flow<String> =
        context.dataStore.data.map { it[stringPreferencesKey(key)] ?: default }
}
