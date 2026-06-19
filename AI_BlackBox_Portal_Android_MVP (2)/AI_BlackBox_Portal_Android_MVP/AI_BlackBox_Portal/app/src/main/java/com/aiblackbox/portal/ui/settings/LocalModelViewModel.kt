package com.aiblackbox.portal.ui.settings

import android.content.Context
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.api.LocalModelApi
import com.aiblackbox.portal.data.api.LocalModelCatalogClient
import com.aiblackbox.portal.data.model.AttestRequest
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
 * @param failedSlugs slugs whose last download attempt FAILED and which are not
 *   (yet) on disk. Drives the per-row FAILED state + a Retry affordance (Task
 *   W5.1); cleared the moment a (re)download starts or succeeds.
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
    val failedSlugs: Set<String> = emptySet(),
    val autonomyMode: String = AUTONOMY_PERMISSION,
    val activeSlug: String? = null,
    val error: String? = null,
) {
    /** True when [slug] already has on-disk bytes. */
    fun isInstalled(slug: String): Boolean = installed.any { it.slug == slug }

    /**
     * The merged picker rows (Task W5.1) -- the catalog joined with the installed
     * set, in-flight downloads, the failed set and the active slug, recommended
     * first. Computed (not stored) so it can never drift from the raw state.
     */
    val rows: List<ModelRow>
        get() = modelRowsFrom(
            catalog = catalog,
            installed = installed,
            downloading = downloadProgress,
            failed = failedSlugs,
            activeSlug = activeSlug,
        )
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
    /**
     * Mirror a successfully-changed autonomy mode into LOCAL persistence
     * (Task 4.6) so the on-device phone-control agent can read it WITHOUT a
     * network round-trip and fail SAFE. Receives the wire string ("yolo"/
     * "permission"). Default no-op keeps the core framework-free + testable; the
     * production [fromContext] wires it to [com.aiblackbox.portal.data.local.AutonomyStore].
     */
    private val onAutonomyPersisted: (String) -> Unit = {},
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
                // Read the hub's posture. On a SUCCESSFUL read, mirror it into LOCAL
                // persistence so the on-device phone-control gate (4.6) has a current
                // value without a network hop. On a FAILED read fall back to
                // PERMISSION for display but do NOT persist it — a transient network
                // blip must not silently clobber a deliberately-stored YOLO.
                val fetchedMode = runCatching {
                    val status = catalog.status(operator)
                    status.models.firstOrNull()?.autonomyMode ?: AUTONOMY_PERMISSION
                }.getOrNull()
                fetchedMode?.let { onAutonomyPersisted(it) }
                val mode = fetchedMode ?: AUTONOMY_PERMISSION

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
                // A (re)download starts -> drop any stale FAILED flag for this slug.
                failedSlugs = it.failedSlugs - bundle.slug,
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
                            // Terminal-wins guard: on a multi-threaded dispatcher a
                            // late progress launch can run AFTER the success/failure
                            // update has removed this slug from downloadProgress and
                            // cleared busySlug. Without this guard it would re-insert
                            // the slug, leaving the row stuck on a progress bar for an
                            // already-installed model. busySlug == this slug for the
                            // whole download and is cleared at both terminal paths, so
                            // it is the authoritative "still in flight" signal.
                            if (s.busySlug != bundle.slug) s
                            else s.copy(downloadProgress = s.downloadProgress + (bundle.slug to fraction))
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
                        // Clear the progress entry + any FAILED flag on success.
                        downloadProgress = it.downloadProgress - bundle.slug,
                        failedSlugs = it.failedSlugs - bundle.slug,
                        error = null,
                    )
                }
            } else {
                _state.update {
                    it.copy(
                        busySlug = null,
                        downloadProgress = it.downloadProgress - bundle.slug,
                        // Mark this slug FAILED so the row shows a Retry affordance
                        // (Task W5.1). The underlying download is resumable, so a
                        // retry resumes the partial .part rather than restarting.
                        failedSlugs = it.failedSlugs + bundle.slug,
                        error = "Download failed: ${result.exceptionOrNull()?.message ?: "unknown error"}",
                    )
                }
            }
        }
    }

    /**
     * Retry a previously-FAILED download (Task W5.1). Identical to [download] --
     * the underlying [LocalModelManager.install] is resumable, so this resumes the
     * partial `.part` rather than restarting the multi-GB fetch, and [download]
     * already clears the slug's FAILED flag on (re)start.
     */
    fun retry(bundle: LocalBundle) = download(bundle)

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
     * Flip the device autonomy posture — **LOCAL-FIRST** (Task W7).
     *
     * The actuator gate ([com.aiblackbox.portal.overlay.Actuators]/[com.aiblackbox.portal.overlay.IntentActuator])
     * reads the LOCAL [com.aiblackbox.portal.data.local.AutonomyStore], so THAT is
     * the authoritative source of truth and must work offline / regardless of the
     * backend. So we:
     *
     *  1. Persist the new mode LOCALLY ([onAutonomyPersisted]) + reflect it in the
     *     UI ([LocalModelUiState.autonomyMode]) **immediately** — the toggle never
     *     blocks on, nor 404s from, the network. The gate is correct at once.
     *  2. Fire a BEST-EFFORT backend mirror (`POST /local/device/autonomy`). On any
     *     failure — offline, 5xx, or a **404 because this device was never attested
     *     in that hub's registry** (e.g. a SIDELOADED model that skipped the
     *     install→attest flow) — we do NOT surface an error: the local toggle still
     *     holds and the gate is already correct.
     *  3. Self-heal: a failed mirror is treated as "probably unattested", so we
     *     [attest][LocalModelCatalogClient.attest] this device ONCE (idempotent;
     *     ledger-ready per-operator binding) carrying the chosen mode, then retry
     *     the mirror. Both steps are swallowed (logged only) — the local posture is
     *     authoritative either way.
     *
     * Net: pressing the toggle NEVER shows the user a 404; the backend mirrors the
     * posture whenever it is reachable + the device is attested.
     */
    fun setAutonomy(mode: String) {
        // LOCAL-FIRST: the gate's source of truth + the UI update happen now,
        // synchronously w.r.t. this call — independent of the backend.
        onAutonomyPersisted(mode)
        _state.update { it.copy(autonomyMode = mode, error = null) }

        // Best-effort backend mirror (never surfaces an error to the user).
        scope.launch {
            runCatching {
                val operator = operatorProvider()
                val ok = catalog.setAutonomy(operator, deviceId, mode)
                if (!ok) {
                    // A false here is most often a 404 for an unattested device
                    // (sideloaded model). Self-heal: attest once (idempotent), then
                    // retry the mirror. Both are best-effort.
                    val attested = catalog.attest(
                        AttestRequest(
                            operator = operator,
                            deviceId = deviceId,
                            autonomyMode = mode,
                            tailnetName = com.aiblackbox.portal.data.remote.TailnetAddress.localTailnetIpv4(),
                        )
                    )
                    if (attested) {
                        catalog.setAutonomy(operator, deviceId, mode)
                    }
                }
            }.onFailure {
                // Swallow: the LOCAL posture (step 1) already holds and the gate is
                // correct. A backend blip must never block or error the toggle.
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
            // Local mirror of the autonomy posture for the on-device phone-control
            // gate (Task 4.6): the toggle writes here so the agent reads it with no
            // network hop and fails SAFE (PERMISSION) when unset.
            val autonomyStore = com.aiblackbox.portal.data.local.AutonomyStore.fromContext(context)
            return LocalModelViewModel(
                installer = manager,
                catalog = localApi,
                operatorProvider = operatorProvider,
                deviceId = deviceId,
                onModelSelected = onModelSelected,
                ioDispatcher = ioDispatcher,
                onAutonomyPersisted = { wire ->
                    autonomyStore.save(com.aiblackbox.portal.data.local.AutonomyStore.parse(wire))
                },
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
