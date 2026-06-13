package com.aiblackbox.portal.ui.generation

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.TaskResponse
import com.aiblackbox.portal.data.model.TaskStatus
import com.aiblackbox.portal.data.repository.TaskRepository
import com.aiblackbox.portal.data.store.BlackBoxStore
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

enum class GenType { IMAGE, VIDEO, MUSIC }
enum class GenState { IDLE, SUBMITTING, POLLING, COMPLETED, FAILED }

class GenerationViewModel(application: Application) : AndroidViewModel(application) {
    private val store = BlackBoxStore(application)
    private var api: BlackBoxApi? = null
    private var taskRepo: TaskRepository? = null

    private val _state = MutableStateFlow(GenState.IDLE)
    val state: StateFlow<GenState> = _state.asStateFlow()

    private val _taskStatus = MutableStateFlow<TaskStatus?>(null)
    val taskStatus: StateFlow<TaskStatus?> = _taskStatus.asStateFlow()

    private val _resultUrl = MutableStateFlow<String?>(null)
    val resultUrl: StateFlow<String?> = _resultUrl.asStateFlow()

    private val _resultUrls = MutableStateFlow<List<String>>(emptyList())
    val resultUrls: StateFlow<List<String>> = _resultUrls.asStateFlow()

    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error.asStateFlow()

    private var currentOperator = "Brandon"

    init {
        viewModelScope.launch { store.operator.collect { currentOperator = it } }
    }

    fun initialize(origin: String) {
        if (origin.isBlank() || api != null) return
        api = BlackBoxApi(origin)
        taskRepo = TaskRepository(api!!)
    }

    fun generateImage(
        prompt: String,
        aspectRatio: String = "1:1",
        numberOfImages: Int = 1
    ) {
        val api = api ?: return
        _state.value = GenState.SUBMITTING
        _error.value = null
        viewModelScope.launch {
            try {
                val body = buildJsonObject {
                    put("prompt", prompt)
                    put("operator", currentOperator)
                    put("aspectRatio", aspectRatio)
                    put("numberOfImages", numberOfImages)
                }.toString()
                val response = api.post("/generate/image", body)
                val taskResponse = api.json.decodeFromString(TaskResponse.serializer(), response)
                pollTask(taskResponse.taskId)
            } catch (e: Exception) {
                _state.value = GenState.FAILED
                _error.value = e.message
            }
        }
    }

    fun generateVideo(
        prompt: String,
        aspectRatio: String = "16:9",
        duration: Int = 6
    ) {
        val api = api ?: return
        _state.value = GenState.SUBMITTING
        _error.value = null
        viewModelScope.launch {
            try {
                val body = buildJsonObject {
                    put("prompt", prompt)
                    put("operator", currentOperator)
                    put("aspectRatio", aspectRatio)
                    put("duration", duration)
                }.toString()
                val response = api.post("/generate/video", body)
                val taskResponse = api.json.decodeFromString(TaskResponse.serializer(), response)
                pollTask(taskResponse.taskId)
            } catch (e: Exception) {
                _state.value = GenState.FAILED
                _error.value = e.message
            }
        }
    }

    fun generateMusic(prompt: String, negativePrompt: String = "") {
        val api = api ?: return
        _state.value = GenState.SUBMITTING
        _error.value = null
        viewModelScope.launch {
            try {
                val body = buildJsonObject {
                    put("prompt", prompt)
                    put("operator", currentOperator)
                    if (negativePrompt.isNotBlank()) put("negative_prompt", negativePrompt)
                }.toString()
                val response = api.post("/generate/lyria_music", body)
                val taskResponse = api.json.decodeFromString(TaskResponse.serializer(), response)
                pollTask(taskResponse.taskId)
            } catch (e: Exception) {
                _state.value = GenState.FAILED
                _error.value = e.message
            }
        }
    }

    /**
     * ElevenLabs Music — full songs up to 5 min, vocals supported, no restricted
     * vocabulary. Mirrors [generateMusic] and reuses the SAME task-result/playback
     * state via [pollTask] so the result UI is shared across both providers.
     */
    fun generateElevenLabsMusic(
        prompt: String,
        musicLengthMs: Int = 30000,
        forceInstrumental: Boolean = false
    ) {
        val api = api ?: return
        _state.value = GenState.SUBMITTING
        _error.value = null
        viewModelScope.launch {
            try {
                val body = buildJsonObject {
                    put("prompt", prompt)
                    put("music_length_ms", musicLengthMs)
                    put("force_instrumental", forceInstrumental)
                    put("operator", currentOperator)
                }.toString()
                val response = api.post("/generate/elevenlabs_music", body)
                val taskResponse = api.json.decodeFromString(TaskResponse.serializer(), response)
                pollTask(taskResponse.taskId)
            } catch (e: Exception) {
                _state.value = GenState.FAILED
                _error.value = e.message
            }
        }
    }

    private fun pollTask(taskId: String) {
        val repo = taskRepo ?: return
        _state.value = GenState.POLLING
        viewModelScope.launch {
            repo.pollTask(taskId).collect { status ->
                _taskStatus.value = status
                when (status.status.uppercase()) {
                    "COMPLETED" -> {
                        _state.value = GenState.COMPLETED
                        _resultUrl.value = status.resultUrl
                    }
                    "FAILED" -> {
                        _state.value = GenState.FAILED
                        _error.value = status.error ?: "Generation failed"
                    }
                }
            }
        }
    }

    fun reset() {
        _state.value = GenState.IDLE
        _taskStatus.value = null
        _resultUrl.value = null
        _resultUrls.value = emptyList()
        _error.value = null
    }
}
