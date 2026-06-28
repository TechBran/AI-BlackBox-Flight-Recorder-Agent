package com.aiblackbox.portal.ui.settings

import android.content.Context
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.api.LocalModelApi
import com.aiblackbox.portal.data.api.LocalModelCatalogClient
import com.aiblackbox.portal.data.model.AttestRequest
import com.aiblackbox.portal.data.local.DownloadProgressBus
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
 * [deviceId], [onModelSelected], [startDownload], [ioDispatcher]) so the whole
 * thing unit-tests with plain JUnit + `runTest` and in-memory fakes — no Context,
 * disk, or network. The Composable builds the production wiring via [fromContext].
 *
 * **Durable downloads (Phase C).** The multi-GB transfer no longer runs inside
 * this holder's [scope] — `dispose()` cancels that scope on screen-leave, which
 * used to kill an in-flight download. [download] now hands the bundle to the
 * [startDownload] seam (production: a foreground
 * [com.aiblackbox.portal.ModelDownloadService] that OUTLIVES this holder), and the
 * Service publishes live progress to the process-wide [DownloadProgressBus]. This
 * holder OBSERVES that bus ([onBus]); because the bus is a StateFlow, a freshly
 * recreated holder's [init] collector immediately replays the latest state and
 * re-attaches to a download that is still running. Progress is throttled to
 * whole-percent ticks by the Service (the producer), so the bus is not chatty.
 */
class LocalModelViewModel(
    private val installer: LocalModelInstaller,
    private val catalog: LocalModelCatalogClient,
    private val operatorProvider: () -> String,
    private val deviceId: String,
    private val onModelSelected: (String) -> Unit,
    /**
     * Start the durable download for a bundle (Phase C). Production ([fromContext])
     * wires this to [com.aiblackbox.portal.ModelDownloadService.start], which runs
     * the transfer in a foreground Service that survives [dispose]; tests inject a
     * fake that drives [DownloadProgressBus] directly. This holder reacts ONLY via
     * the bus, never by awaiting the download itself.
     */
    private val startDownload: (LocalBundle) -> Unit,
    ioDispatcher: CoroutineDispatcher = Dispatchers.Main,
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

    init {
        // Observe the durable-download bus. A StateFlow replays its latest value on
        // subscription, so a holder created AFTER a download started (e.g. the user
        // navigated away + back, recreating this VM) immediately re-attaches to the
        // in-flight (or already-terminal) state — the download itself runs in the
        // Service, independent of this scope.
        scope.launch { DownloadProgressBus.flow.collect { onBus(it) } }
    }

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
     * Start a durable download of [bundle] (Phase C). Seeds the busy/progress UI
     * state, then hands the transfer to the [startDownload] seam — the foreground
     * [com.aiblackbox.portal.ModelDownloadService], which OUTLIVES this holder, so
     * leaving the screen ([dispose]) no longer cancels the download. The actual
     * progress + the terminal install/verify/attest result flow back through
     * [DownloadProgressBus] into [onBus]; this method never awaits the download.
     *
     * Resumable: a re-tap after a failed attempt resumes the partial `.part`
     * (LocalModelApi.download), so we do NOT permanently lock the slug — one
     * in-flight action at a time per slug (guarded by [LocalModelUiState.busySlug]).
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
        // The Service throttles progress + runs install/verify/attest and publishes
        // RUNNING/SUCCESS/FAILED to DownloadProgressBus, which this VM observes (onBus).
        startDownload(bundle)
    }

    /**
     * React to the durable-download [DownloadProgressBus]. Runs in this holder's
     * [scope]; a freshly recreated holder re-attaches automatically because the bus
     * is a StateFlow (the [init] collector replays the latest value). Per slug:
     *  - RUNNING -> reflect fractional progress; adopt it as [busySlug] if nothing
     *    else is busy (so a re-attached holder shows the in-flight row correctly).
     *  - SUCCESS -> re-scan installed, clear this slug's progress/failed/busy, drop
     *    the error, then [DownloadProgressBus.clear] the consumed terminal state.
     *  - FAILED  -> mark the slug failed, clear its progress/busy, surface the error,
     *    then clear the consumed terminal state from the bus.
     */
    private suspend fun onBus(map: Map<String, DownloadProgressBus.State>) {
        for ((slug, st) in map) {
            when (st.status) {
                DownloadProgressBus.Status.RUNNING -> _state.update {
                    it.copy(
                        downloadProgress = it.downloadProgress + (slug to st.fraction),
                        // Adopt busy only when free, so we don't clobber another row's
                        // in-flight action; covers the fresh-VM re-attach (busySlug null).
                        busySlug = it.busySlug ?: slug,
                    )
                }
                DownloadProgressBus.Status.SUCCESS -> {
                    val installedList = runCatching { installer.installedModels() }
                        .getOrDefault(_state.value.installed)
                    _state.update {
                        it.copy(
                            installed = installedList,
                            busySlug = if (it.busySlug == slug) null else it.busySlug,
                            downloadProgress = it.downloadProgress - slug,
                            failedSlugs = it.failedSlugs - slug,
                            error = null,
                        )
                    }
                    // Consume the terminal state so a re-subscribing VM doesn't replay it.
                    DownloadProgressBus.clear(slug)
                }
                DownloadProgressBus.Status.FAILED -> {
                    _state.update {
                        it.copy(
                            busySlug = if (it.busySlug == slug) null else it.busySlug,
                            downloadProgress = it.downloadProgress - slug,
                            // Mark FAILED so the row shows a Retry affordance (Task W5.1);
                            // the .part lets a retry resume rather than restart.
                            failedSlugs = it.failedSlugs + slug,
                            error = "Download failed: ${st.error ?: "unknown error"}",
                        )
                    }
                    DownloadProgressBus.clear(slug)
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

        /**
         * Production wiring. Builds the real [LocalModelApi] + [LocalModelManager]
         * from a [BlackBoxApi] and a [Context], deriving a stable device id from
         * ANDROID_ID (matching how the manager factory keeps all framework access
         * out of the testable core). The active-model selection is recorded via
         * [onModelSelected] — 1.6 hands in a setter into the model store.
         *
         * The [startDownload] seam is wired to [com.aiblackbox.portal.ModelDownloadService]
         * (a foreground Service) so the multi-GB transfer survives this holder's
         * [dispose]; it is handed the same `origin` + `deviceId` this factory resolved
         * so the Service constructs the identical [LocalModelManager].
         */
        fun fromContext(
            context: Context,
            api: BlackBoxApi,
            operatorProvider: () -> String,
            onModelSelected: (String) -> Unit,
            ioDispatcher: CoroutineDispatcher = Dispatchers.Main,
        ): LocalModelViewModel {
            val deviceId = com.aiblackbox.portal.util.DeviceId.stable(context)
            val localApi = LocalModelApi(api)
            val manager = LocalModelManager.fromContext(context, localApi, deviceId)
            // GPU/CPU delegate the installed model is configured for. The download
            // itself is delegate-agnostic (bytes only); it is forwarded to the Service
            // so it builds the same manager/config the ViewModel would.
            val delegate = "cpu"
            val appContext = context.applicationContext
            val origin = api.getBaseUrl()
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
                startDownload = { b ->
                    com.aiblackbox.portal.ModelDownloadService.start(
                        appContext, b, operatorProvider(), delegate, origin, deviceId,
                    )
                },
                ioDispatcher = ioDispatcher,
                onAutonomyPersisted = { wire ->
                    autonomyStore.save(com.aiblackbox.portal.data.local.AutonomyStore.parse(wire))
                },
            )
        }

    }
}
