package com.aiblackbox.portal.ui.settings

import android.content.Context
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.api.LocalModelApi
import com.aiblackbox.portal.data.api.LocalModelCatalogClient
import com.aiblackbox.portal.data.local.InstalledModel
import com.aiblackbox.portal.data.local.LocalModelInstaller
import com.aiblackbox.portal.data.local.LocalModelManager
import com.aiblackbox.portal.data.model.LocalBundle
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * Immutable UI state for the on-device Gemma Model Manager section
 * ([LocalModelSection], Task 1.5).
 *
 * @param catalog downloadable bundles from GET /local/models/catalog.
 * @param installed bundles present on disk (by slug; size in bytes).
 * @param recommendedSlug the bundle [LocalModelManager.recommendForDevice]
 *   picks for this phone's RAM — drives the "Recommended for your phone" badge.
 * @param downloadProgress per-slug fractional progress in 0f..1f while a
 *   download is in flight; an entry is removed when the install finishes. A slug
 *   with no entry is "not downloading". -1f sentinel = indeterminate (total
 *   unknown), rendered as a busy spinner.
 * @param busySlug the slug whose long-running action (install/delete) is in
 *   flight; gates re-taps on that row (but a failed download is retryable once
 *   cleared).
 * @param autonomyMode "permission" (asks before high-consequence phone actions)
 *   or "yolo" (full autonomy). Defaults to "permission".
 * @param activeSlug the slug the user has selected as the active local model
 *   (recorded for the picker in Task 1.6); null until chosen.
 * @param error a user-facing error string, or null.
 */
data class LocalModelUiState(
    val catalog: List<LocalBundle> = emptyList(),
    val installed: List<InstalledModel> = emptyList(),
    val recommendedSlug: String? = null,
    val downloadProgress: Map<String, Float> = emptyMap(),
    val busySlug: String? = null,
    val autonomyMode: String = AUTONOMY_PERMISSION,
    val activeSlug: String? = null,
    val error: String? = null,
) {
    /** True when [slug] already has on-disk bytes. */
    fun isInstalled(slug: String): Boolean = installed.any { it.slug == slug }
}

/** Autonomy posture: asks before high-consequence phone actions. */
const val AUTONOMY_PERMISSION = "permission"

/** Autonomy posture: full autonomy, no per-action prompts. */
const val AUTONOMY_YOLO = "yolo"

/**
 * Screen state holder for the Model Manager UI (Task 1.5).
 *
 * **Plain class, not AndroidViewModel** — matches this project's testable
 * holder convention (`CliAgentScreenState`): all Android-framework facts come in
 * through constructor seams ([installer], [catalog], [operatorProvider],
 * [deviceId], [onModelSelected], [ioDispatcher]) so the whole thing unit-tests
 * with plain JUnit + `runTest` and in-memory fakes — no Context, disk, or
 * network. The Composable builds the production wiring via [fromContext].
 *
 * **Progress throttling.** The real download fires [onProgress] on every ~64KB
 * chunk (thousands of callbacks for a multi-GB bundle). Pushing each to Compose
 * state would thrash recomposition. We coalesce: a progress callback only
 * updates state when the fraction advances by ≥ [PROGRESS_STEP] (1%) or hits
 * 1.0, and the [download] action marshals the update onto [scope] so Compose
 * state is touched on the holder's dispatcher, never the IO callback thread.
 */
class LocalModelViewModel(
    private val installer: LocalModelInstaller,
    private val catalog: LocalModelCatalogClient,
    private val operatorProvider: () -> String,
    private val deviceId: String,
    private val onModelSelected: (String) -> Unit,
    ioDispatcher: CoroutineDispatcher = Dispatchers.Main,
    private val delegate: String = "cpu",
) {
    private val scope = CoroutineScope(SupervisorJob() + ioDispatcher)

    private val _state = MutableStateFlow(LocalModelUiState())
    val state: StateFlow<LocalModelUiState> = _state.asStateFlow()

    /**
     * Load everything the section renders: the downloadable catalog, the
     * on-disk installed set, the device autonomy + availability (status), and
     * the RAM-based recommendation. Best-effort: a network miss leaves the
     * installed list + a friendly error but never throws.
     */
    fun refresh() {
        scope.launch {
            try {
                val operator = operatorProvider()
                val bundles = catalog.catalog()
                val installedList = installer.installedModels()
                val recommended = if (bundles.isNotEmpty())
                    runCatching { installer.recommendForDevice(bundles).slug }.getOrNull()
                else null
                val mode = runCatching {
                    val status = catalog.status(operator)
                    status.models.firstOrNull()?.autonomyMode ?: AUTONOMY_PERMISSION
                }.getOrDefault(AUTONOMY_PERMISSION)

                _state.update {
                    it.copy(
                        catalog = bundles,
                        installed = installedList,
                        recommendedSlug = recommended,
                        autonomyMode = mode,
                        error = null,
                    )
                }
            } catch (e: Exception) {
                // Still surface whatever is on disk so the user can manage it.
                val installedList = runCatching { installer.installedModels() }.getOrDefault(emptyList())
                _state.update {
                    it.copy(
                        installed = installedList,
                        error = "Couldn't load model catalog: ${e.message}",
                    )
                }
            }
        }
    }

    /**
     * Download + verify + attest [bundle]. Resumable: a re-tap after a failed
     * attempt resumes the partial `.part` (LocalModelApi.download), so we do NOT
     * permanently lock the slug — only one in-flight action at a time per slug
     * (guarded by [LocalModelUiState.busySlug]).
     *
     * Progress is throttled (see class kdoc) and marshalled onto [scope].
     */
    fun download(bundle: LocalBundle) {
        if (_state.value.busySlug == bundle.slug) return
        _state.update {
            it.copy(
                busySlug = bundle.slug,
                error = null,
                downloadProgress = it.downloadProgress + (bundle.slug to 0f),
            )
        }
        scope.launch {
            // Throttle: only push a state update when progress advances ≥ 1%
            // (or hits 100%). lastEmitted starts below 0 so the first tick lands.
            var lastEmitted = -1f
            val onProgress: (Long, Long) -> Unit = { soFar, total ->
                val fraction = when {
                    total <= 0L -> PROGRESS_INDETERMINATE
                    else -> (soFar.toFloat() / total.toFloat()).coerceIn(0f, 1f)
                }
                val advancedEnough = fraction == PROGRESS_INDETERMINATE ||
                    fraction >= 1f ||
                    fraction - lastEmitted >= PROGRESS_STEP
                if (advancedEnough && fraction != lastEmitted) {
                    lastEmitted = fraction
                    // Marshal onto the holder's dispatcher: download() runs the
                    // callback on an IO thread, so never touch state inline.
                    scope.launch {
                        _state.update { s ->
                            s.copy(downloadProgress = s.downloadProgress + (bundle.slug to fraction))
                        }
                    }
                }
            }

            val result = installer.install(
                bundle = bundle,
                operator = operatorProvider(),
                delegate = delegate,
                onProgress = onProgress,
            )

            if (result.isSuccess) {
                val installedList = runCatching { installer.installedModels() }
                    .getOrDefault(_state.value.installed)
                _state.update {
                    it.copy(
                        installed = installedList,
                        busySlug = null,
                        // Clear the progress entry on success.
                        downloadProgress = it.downloadProgress - bundle.slug,
                        error = null,
                    )
                }
            } else {
                _state.update {
                    it.copy(
                        busySlug = null,
                        downloadProgress = it.downloadProgress - bundle.slug,
                        error = "Download failed: ${result.exceptionOrNull()?.message ?: "unknown error"}",
                    )
                }
            }
        }
    }

    /** Delete an installed model, then refresh the installed list. */
    fun delete(slug: String) {
        if (_state.value.busySlug == slug) return
        _state.update { it.copy(busySlug = slug, error = null) }
        scope.launch {
            try {
                installer.delete(slug)
                val installedList = installer.installedModels()
                val active = if (_state.value.activeSlug == slug) null else _state.value.activeSlug
                _state.update { it.copy(installed = installedList, activeSlug = active, busySlug = null) }
            } catch (e: Exception) {
                _state.update { it.copy(busySlug = null, error = "Delete failed: ${e.message}") }
            }
        }
    }

    /**
     * Flip the device autonomy posture (POST /local/device/autonomy). On
     * success update [LocalModelUiState.autonomyMode]; on rejection leave the
     * mode and surface an error.
     */
    fun setAutonomy(mode: String) {
        scope.launch {
            try {
                val ok = catalog.setAutonomy(operatorProvider(), deviceId, mode)
                if (ok) {
                    _state.update { it.copy(autonomyMode = mode, error = null) }
                } else {
                    _state.update { it.copy(error = "Couldn't change autonomy mode.") }
                }
            } catch (e: Exception) {
                _state.update { it.copy(error = "Couldn't change autonomy mode: ${e.message}") }
            }
        }
    }

    /**
     * Record [slug] as the active local model. Persisting the choice into the
     * picker is Task 1.6's job — here we just record the selection (callback +
     * state) so 1.6 can wire it.
     */
    fun switchModel(slug: String) {
        _state.update { it.copy(activeSlug = slug) }
        onModelSelected(slug)
    }

    /** Clear a surfaced error (e.g. after showing it). */
    fun clearError() {
        _state.update { it.copy(error = null) }
    }

    /** Tear down the holder's coroutine scope (call from the Composable's DisposableEffect). */
    fun dispose() {
        scope.cancel()
    }

    companion object {
        /** Minimum fractional advance (1%) before a progress tick updates state. */
        const val PROGRESS_STEP = 0.01f

        /** Sentinel fraction for "total unknown" → render an indeterminate spinner. */
        const val PROGRESS_INDETERMINATE = -1f

        /**
         * Production wiring. Builds the real [LocalModelApi] + [LocalModelManager]
         * from a [BlackBoxApi] and a [Context], deriving a stable device id from
         * ANDROID_ID (matching how the manager factory keeps all framework access
         * out of the testable core). The active-model selection is recorded via
         * [onModelSelected] — 1.6 hands in a setter into the model store.
         */
        fun fromContext(
            context: Context,
            api: BlackBoxApi,
            operatorProvider: () -> String,
            onModelSelected: (String) -> Unit,
            ioDispatcher: CoroutineDispatcher = Dispatchers.Main,
        ): LocalModelViewModel {
            val deviceId = stableDeviceId(context)
            val localApi = LocalModelApi(api)
            val manager = LocalModelManager.fromContext(context, localApi, deviceId)
            return LocalModelViewModel(
                installer = manager,
                catalog = localApi,
                operatorProvider = operatorProvider,
                deviceId = deviceId,
                onModelSelected = onModelSelected,
                ioDispatcher = ioDispatcher,
            )
        }

        /** Stable per-device id: ANDROID_ID, falling back to a constant. */
        @Suppress("HardwareIds")
        private fun stableDeviceId(context: Context): String {
            val androidId = runCatching {
                android.provider.Settings.Secure.getString(
                    context.contentResolver,
                    android.provider.Settings.Secure.ANDROID_ID,
                )
            }.getOrNull()
            return androidId?.takeIf { it.isNotBlank() } ?: "android-device"
        }
    }
}
