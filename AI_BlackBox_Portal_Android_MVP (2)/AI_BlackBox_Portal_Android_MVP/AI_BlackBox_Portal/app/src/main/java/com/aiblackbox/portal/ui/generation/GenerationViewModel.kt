package com.aiblackbox.portal.ui.generation

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.TaskResponse
import com.aiblackbox.portal.data.model.TaskStatus
import com.aiblackbox.portal.data.repository.LOST_CONNECTION_MESSAGE
import com.aiblackbox.portal.data.repository.TaskPollOutcome
import com.aiblackbox.portal.data.repository.TaskPoller
import com.aiblackbox.portal.data.repository.ImageCatalogProvider
import com.aiblackbox.portal.data.repository.ImageCatalogRepository
import com.aiblackbox.portal.data.repository.awaitTaskOutcome
import com.aiblackbox.portal.data.store.BlackBoxStore
import com.aiblackbox.portal.data.store.OperatorUnresolvedException
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

enum class GenType { IMAGE, VIDEO, MUSIC }
// CANCELLED is terminal (A11-settings-generation-02): Settings > Cancel Tasks or a
// per-task cancel marks the row cancelled, and the screen must stop spinning.
// LOST: the poll ran out of retries but the task keeps running on the box. Its button
// is "Check again" (GenerationViewModel.resumePolling, the same task id), never the
// FAILED "Retry", which submits a new, paid generation.
enum class GenState { IDLE, SUBMITTING, POLLING, COMPLETED, FAILED, CANCELLED, LOST }

/**
 * [url] made absolute against the paired [origin] (A11-settings-generation-03). The box
 * stores result_url as a box-relative "/ui/uploads/<file>" path, which Coil and
 * MediaPlayer read as a local FILE, so generated media never rendered or played. An
 * absolute URL passes through; blank input, or no usable origin, gives null.
 */
fun absoluteMediaUrl(origin: String, url: String?): String? {
    val u = url?.trim().orEmpty()
    if (u.isEmpty()) return null
    if (u.startsWith("http://", ignoreCase = true) || u.startsWith("https://", ignoreCase = true)) return u
    val base = origin.trim().toHttpUrlOrNull() ?: return null
    return base.resolve(u)?.toString()
}

/**
 * Every media URL a finished task produced, absolute: result_data.all_urls (all N images
 * of a multi-image request, as the Portal renders them), else the single result_url.
 */
fun mediaUrlsFor(status: TaskStatus?, origin: String): List<String> {
    if (status == null) return emptyList()
    val all = ((status.resultData as? JsonObject)?.get("all_urls") as? JsonArray)
        ?.mapNotNull { (it as? JsonPrimitive)?.contentOrNull }
        ?.takeIf { it.isNotEmpty() }
    return (all ?: listOfNotNull(status.resultUrl))
        .mapNotNull { absoluteMediaUrl(origin, it) }
        .distinct()
}

class GenerationViewModel(application: Application) : AndroidViewModel(application) {
    private val store = BlackBoxStore(application)
    private var api: BlackBoxApi? = null
    private var poller: TaskPoller? = null
    private var imageCatalogRepo: ImageCatalogRepository? = null

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

    // --- Provider-aware image catalog (mirrors Portal's /image/catalog hydration) ---
    private val _imageProviders = MutableStateFlow<List<ImageCatalogProvider>>(emptyList())
    val imageProviders: StateFlow<List<ImageCatalogProvider>> = _imageProviders.asStateFlow()

    private val _selectedImageProvider = MutableStateFlow<String?>(null)
    val selectedImageProvider: StateFlow<String?> = _selectedImageProvider.asStateFlow()

    /** Current param values keyed by catalog param name (e.g. "aspectRatio" -> "16:9"). */
    private val _imageParamValues = MutableStateFlow<Map<String, String>>(emptyMap())
    val imageParamValues: StateFlow<Map<String, String>> = _imageParamValues.asStateFlow()

    // The running task poll. Kept so reset() and the next submit cancel it — before,
    // an abandoned poll kept writing its late result over the fresh screen.
    private var pollJob: Job? = null
    // The task [pollJob] polls (or last polled), for "Check again" after a LOST poll.
    private var polledTaskId: String? = null

    /**
     * The operator for THIS request, read from the store now (never a seed name); with
     * none resolved the submit fails with the reason instead of going out under nobody.
     */
    private suspend fun operatorForRequest(): String =
        store.operator.first().ifBlank { throw OperatorUnresolvedException() }

    // The paired origin, which result URLs are made absolute against.
    private var origin: String = ""

    fun initialize(origin: String) {
        if (origin.isBlank() || api != null) return
        this.origin = origin
        api = BlackBoxApi(origin)
        poller = TaskPoller(api!!)
        imageCatalogRepo = ImageCatalogRepository(api!!)
        loadImageCatalog()
    }

    /**
     * Load the image provider catalog (fail-open). Preselects the default
     * provider and seeds its param values from each spec's default. Mirrors
     * Portal setupImageProviderControls().
     */
    private fun loadImageCatalog() {
        val repo = imageCatalogRepo ?: return
        viewModelScope.launch {
            val catalog = repo.fetchCatalog()
            _imageProviders.value = catalog
            // Preselect default (default:true, else first), keep prior pick if still valid.
            val current = _selectedImageProvider.value
            val keep = catalog.any { it.provider == current }
            val chosen = when {
                keep -> current
                else -> (catalog.firstOrNull { it.default } ?: catalog.firstOrNull())?.provider
            }
            if (chosen != null) selectImageProvider(chosen)
        }
    }

    /**
     * Select an image provider and reset the param values to that provider's
     * spec defaults (so the controls SWAP when the dropdown changes).
     */
    fun selectImageProvider(provider: String) {
        _selectedImageProvider.value = provider
        val entry = _imageProviders.value.firstOrNull { it.provider == provider }
        _imageParamValues.value = entry?.params?.mapNotNull { spec ->
            val d = spec.default ?: spec.options.firstOrNull()
            if (d != null) spec.name to d else null
        }?.toMap() ?: emptyMap()
    }

    /** Update a single param value (keyed by catalog param name). */
    fun setImageParam(name: String, value: String) {
        _imageParamValues.value = _imageParamValues.value.toMutableMap().apply { put(name, value) }
    }

    /**
     * Provider-aware image generation. POSTs /generate/image with {prompt,
     * operator, provider, ...params}. Param names (aspectRatio/resolution/
     * numberOfImages/size/quality) come straight from the catalog spec so they
     * match the backend GenIn model. int-typed params are sent as numbers.
     */
    fun generateImageForProvider(
        prompt: String,
        provider: String,
        params: Map<String, String>,
        intParamNames: Set<String> = emptySet(),
    ) {
        val api = api ?: return
        _state.value = GenState.SUBMITTING
        _error.value = null
        viewModelScope.launch {
            try {
                val body = buildJsonObject {
                    put("prompt", prompt)
                    put("operator", operatorForRequest())
                    put("provider", provider)
                    params.forEach { (name, value) ->
                        if (name in intParamNames) {
                            put(name, JsonPrimitive(value.toIntOrNull() ?: 1))
                        } else {
                            put(name, value)
                        }
                    }
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
                    put("operator", operatorForRequest())
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
                    put("operator", operatorForRequest())
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
                    put("operator", operatorForRequest())
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
                    put("operator", operatorForRequest())
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

    /**
     * Poll [taskId] to its end. A dropped connection no longer escapes this launch
     * (A11-settings-generation-01 — it killed the process): the shared poller retries
     * transient failures and ends every poll as an outcome, mapped by [genResultFor].
     * The result URL comes back absolute: the box stores `/ui/uploads/...`, which the
     * image loader and the players cannot open on their own.
     */
    private fun pollTask(taskId: String) {
        val p = poller ?: return
        pollJob?.cancel()
        polledTaskId = taskId
        _state.value = GenState.POLLING
        _error.value = null
        pollJob = viewModelScope.launch {
            val result = pollToResult(p.poll(taskId), p::absolute) { status -> _taskStatus.value = status }
            if (result.state == GenState.COMPLETED) {
                // Absolute, and every image of a multi-image result (A11-settings-generation-03).
                val urls = mediaUrlsFor(_taskStatus.value, origin)
                _resultUrls.value = urls
                (urls.firstOrNull() ?: absoluteMediaUrl(origin, result.resultUrl))?.let { _resultUrl.value = it }
            }
            _state.value = result.state
            _error.value = result.error
        }
    }

    /**
     * "Check again" after a LOST poll: poll the SAME task again — the box kept running
     * it, and its result may be ready — instead of submitting a new generation.
     */
    fun resumePolling() {
        val taskId = resumableTaskId(_state.value, polledTaskId) ?: return
        pollTask(taskId)
    }

    fun reset() {
        pollJob?.cancel()
        pollJob = null
        polledTaskId = null
        _state.value = GenState.IDLE
        _taskStatus.value = null
        _resultUrl.value = null
        _resultUrls.value = emptyList()
        _error.value = null
    }

    /** What a finished poll leaves on screen. */
    data class GenResult(val state: GenState, val resultUrl: String?, val error: String?)

    companion object {
        /** Collect [poll] to its end (statuses to [onStatus]) and map it — the poller's pure core. */
        internal suspend fun pollToResult(
            poll: Flow<TaskStatus>,
            absolutize: (String?) -> String? = { it },
            onStatus: (TaskStatus) -> Unit = {},
        ): GenResult = genResultFor(awaitTaskOutcome(poll, absolutize, onStatus))

        internal fun genResultFor(outcome: TaskPollOutcome): GenResult = when (outcome) {
            is TaskPollOutcome.Completed -> GenResult(GenState.COMPLETED, outcome.resultUrl, null)
            is TaskPollOutcome.Failed -> GenResult(GenState.FAILED, null, outcome.message)
            TaskPollOutcome.Cancelled -> GenResult(GenState.CANCELLED, null, null)
            // Only the connection was lost; the task keeps running on the box and its
            // result still lands there (the media library lists it).
            TaskPollOutcome.LostConnection -> GenResult(GenState.LOST, null, LOST_CONNECTION_MESSAGE)
        }

        /** The task "Check again" re-polls: only a LOST poll's, else null. */
        internal fun resumableTaskId(state: GenState, taskId: String?): String? =
            if (state == GenState.LOST) taskId else null
    }
}
