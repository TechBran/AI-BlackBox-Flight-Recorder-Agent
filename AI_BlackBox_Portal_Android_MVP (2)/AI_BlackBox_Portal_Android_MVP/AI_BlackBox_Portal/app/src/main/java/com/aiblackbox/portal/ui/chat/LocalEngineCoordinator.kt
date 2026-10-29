package com.aiblackbox.portal.ui.chat

import android.content.Context
import android.util.Log
import com.aiblackbox.portal.LocalModelService
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.api.LocalModelApi
import com.aiblackbox.portal.data.local.EngineSource
import com.aiblackbox.portal.data.local.LiteRtEngine
import com.aiblackbox.portal.data.local.LocalEngineHolder
import com.aiblackbox.portal.data.local.LocalLlm
import com.aiblackbox.portal.data.local.LocalModelManager
import com.aiblackbox.portal.data.local.SamplerSettings
import com.aiblackbox.portal.data.local.engineSourceFor
import com.aiblackbox.portal.data.local.pinWarmEngine
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

private const val TAG = "ChatVM"

/**
 * The chat's side of the on-device (Gemma) engine, out of ChatViewModel (audit
 * A04-chat-viewmodel-17, rank 76): wiring the engine for the active bundle (borrowing the
 * one [LocalEngineHolder] pins, or building one), loading it through the one process warm
 * ([pinWarmEngine], M4's single owner, rank 63), the warm-while-open preload and its
 * [state], the deferred re-warm a model switch or a settings apply waits for when a turn
 * is generating, and the settings apply itself. ChatViewModel keeps the turns and calls
 * in; its public engine API delegates here.
 *
 * [api] is the box once ChatViewModel.initialize set it; [isLocalProvider] says whether the
 * on-device provider is the active one; [turnInFlight] whether an on-device turn is
 * generating (STREAMING or THINKING), when the native engine must not be closed.
 */
class LocalEngineCoordinator(
    private val appContext: Context,
    private val scope: CoroutineScope,
    private val api: () -> BlackBoxApi?,
    private val isLocalProvider: () -> Boolean,
    private val turnInFlight: () -> Boolean,
) {
    // Warm-loop guard: a disk-persisted "warm in-flight" flag set BEFORE every
    // on-device engine warm and cleared on its success / graceful failure. A
    // SIGKILL mid-warm (device OOM) runs no cleanup, so the flag survives still-set
    // into the next process start -- the AUTO-warm in [preload] reads it
    // and SKIPS, turning a crash/restart/auto-warm loop into a single failure
    // (a deliberate send is the manual retry). See [WarmInflightStore].
    private val warmInflightStore by lazy {
        com.aiblackbox.portal.data.local.WarmInflightStore.fromContext(appContext)
    }

    // Auto-warm-on-open SETTING (user preference, persisted): when DISABLED the
    // [preload] auto path skips the warm and the model loads lazily on the
    // first send. Default TRUE (instant first send). Distinct from the warm-loop
    // GUARD above, which is an automatic OOM-crash safety, not a user choice.
    // See [LocalWarmPrefs].
    private val localWarmPrefs by lazy {
        com.aiblackbox.portal.data.local.LocalWarmPrefs.fromContext(appContext)
    }

    // ── On-device (local) engine seam (Task 2.4) ──
    // The on-device generation engine, injected as a lazy provider so tests can
    // supply a FakeLocalLlm. NULL until an installed on-device model is found —
    // then it is lazily wired (Task 2.6a) to the SINGLETON [engine]
    // (initialize() is ~10s, so the engine is built once and reused across turns).
    // With no model installed it stays null and the chat falls back to the 1.6
    // placeholder. Settable so a host/builder (or a test) can wire a fake.
    // @Volatile (A04-chat-viewmodel-09): the engine fields are written by the wiring
    // (which may run on an IO thread for the preload) and read on Main.
    @Volatile
    var llmProvider: (() -> LocalLlm)? = null

    // The concrete on-device engine (Task 2.6a). Wired by [providerOrWire] —
    // borrowed from [LocalEngineHolder], or built for the active bundle and then loaded
    // through [pinWarmEngine], which pins it in the holder (the engine's one owner,
    // audit rank 63). Reused across turns. Until that load, a freshly built engine is
    // an unloaded wrapper this coordinator may drop or close.
    @Volatile
    var engine: LiteRtEngine? = null
        private set

    // The installed bundle file the [engine] runs, and its delegate. Resolved
    // from [LocalModelManager.installedModels] when the engine is wired.
    @Volatile
    private var engineModelFile: java.io.File? = null
    @Volatile
    private var engineDelegate: String = "gpu"

    // True once [engine] is the engine pinned in [LocalEngineHolder] (borrowed, or
    // loaded here through [pinWarmEngine]); the holder owns it, so [release] /
    // [invalidate] must not close it. False only for a built-but-not-loaded
    // wrapper, which holds no native engine.
    @Volatile
    private var engineFromHolder: Boolean = false

    // Single-flight engine wiring (A04-chat-viewmodel-09): the preload (IO) and a send
    // (Main) both wire the engine; the second waits and reuses the first's result
    // instead of building a second engine.
    private val engineWiringMutex = Mutex()

    // The in-flight preload warm, joined before a re-warm (see [reWarm]).
    private var warmJob: Job? = null

    // A settings apply that arrived mid-turn also releases the holder's engine when the
    // deferred re-warm runs (A04-chat-viewmodel-08), so the new window is not lost.
    @Volatile
    private var pendingHolderClose: Boolean = false

    // The slug of the active on-device model the user picked in the Model Manager
    // (Task W5), persisted under "model_local" (ChatViewModel's collector sets it).
    // [providerOrWire] prefers the INSTALLED bundle whose slug matches this, so
    // picking a different installed model among several actually changes which one is
    // warmed/used. Blank -> fall back to the alphabetically-first installed bundle.
    @Volatile
    var currentModelSlug: String = ""

    // I1 (W5 review): a model-selection change that arrives WHILE a local turn is
    // streaming must NOT close the live native engine mid-generation (close() is
    // not serialized against generate() -> native teardown race). When that
    // happens the flag is set and the invalidate+re-warm deferred until the turn
    // completes ([processPendingReWarm]).
    @Volatile
    var pendingReWarm: Boolean = false

    // ── On-device (local) engine readiness (Task W1) ──
    // Mirrors the warm-while-app-open preload so the provider pill can show
    // "loading…/ready". IDLE until a warm starts (we only warm when the local
    // provider is the active one — see [preload]); the lazy load in the chat's
    // on-device turn remains the fallback if the user sends before READY.
    private val _state = MutableStateFlow(LocalEngineState.IDLE)
    val state: StateFlow<LocalEngineState> = _state.asStateFlow()

    // True while a [preload] warm is in flight, so a duplicate trigger
    // (e.g. re-selecting the local provider) does not launch a second concurrent
    // warm. Paired with the model path so a DIFFERENT model can still re-warm.
    @Volatile
    private var warmingModelPath: String? = null

    /**
     * The view model is going away: release the on-device engine's native runtime (Task
     * 2.6a). Safe even if never loaded; close() is idempotent. Guarded so a native-layer
     * throw on teardown can't crash the VM disposal.
     *
     * R2-C: close ONLY an engine this coordinator owns. When [engineFromHolder] the
     * engine is the PROCESS-held warm engine owned by [LocalModelService]; closing it
     * here would tear down the native runtime the service is keeping resident (and break
     * the next turn / the model-as-a-tool path). The service releases it on its own
     * stop/destroy via the holder.
     */
    fun release() {
        if (!engineFromHolder) {
            runCatching { engine?.close() }
        }
        engine = null
        // Every loaded engine is pinned in the holder (audit rank 63). With the service
        // not running nothing else keeps it, so release it here as the VM-owned engine
        // used to be; a running generation keeps it alive until it ends.
        if (!LocalModelService.isRunning()) runCatching { LocalEngineHolder.clearAndClose() }
    }

    /**
     * Resolve the on-device [llmProvider] (Task 2.6a), lazily wiring the
     * SINGLETON production [LiteRtEngine] the first time an INSTALLED model is
     * present.
     *
     *  - If [llmProvider] is already set (a test fake, or a previously-wired
     *    engine) it is returned unchanged — the test seam is never disturbed.
     *  - Otherwise this reads the installed bundles via [LocalModelManager]
     *    ([installedModels] is device-scoped + runs on IO), picks the first
     *    attested/installed bundle, builds the engine singleton ([LiteRtEngine.fromInstalled],
     *    delegate "cpu"), records its model file + delegate for [load], sets
     *    [llmProvider] to return that singleton, and returns it.
     *  - If NO model is installed (or the api is not ready) it returns null and
     *    [llmProvider] stays null — the caller falls back to the placeholder.
     *
     * The engine is built ONCE (its ~10s initialize() happens in [load], also
     * once) and reused across turns. Suspends only for the disk read.
     */
    suspend fun providerOrWire(): (() -> LocalLlm)? = engineWiringMutex.withLock {
        // Single-flight (A04-chat-viewmodel-09): a second caller waits here and gets the
        // first caller's wiring instead of building a second engine.
        llmProvider ?: wire()
    }

    /**
     * Load the wired production engine through the ONE process warm
     * ([pinWarmEngine]: serialized with the service and control_phone warms, bracketed
     * by the warm marker and the crash-loop flag, pinned in [LocalEngineHolder]) and
     * adopt the engine it settles on — this one, or the one another warmer already
     * pinned for the same bundle. A no-op for a test fake (no model file).
     */
    suspend fun loadWired() {
        val wired = engine ?: return
        val modelFile = engineModelFile ?: return
        val pinned = pinWarmEngine(appContext, wired, modelFile.absolutePath, engineDelegate)
        engine = pinned
        engineFromHolder = true
    }

    private suspend fun wire(): (() -> LocalLlm)? {
        val client = api() ?: return null

        // Device-scoped model discovery (deviceId is irrelevant to installedModels()
        // — it only matters for attest/setAutonomy — so a stable constant is fine
        // here; the picker/Model-Manager own the real attest flow).
        val manager = LocalModelManager.fromContext(appContext, LocalModelApi(client), deviceId = "android-device")
        val installed = runCatching { manager.installedModels() }.getOrDefault(emptyList())
        // Task W5: honor the user's ACTIVE on-device model selection (persisted under
        // "model_local" -> [currentModelSlug]). Pick the installed bundle whose
        // slug matches it; if unset or no longer installed (e.g. just deleted), fall
        // back to installedModels()'s alphabetically-first entry (gemma-4-e2b, the
        // LIGHTER model) -- a safe RAM default and the prior behavior.
        val bundle = installed.firstOrNull { it.slug == currentModelSlug }
            ?: installed.firstOrNull()
            ?: return null // no model -> placeholder path

        val delegate = "gpu" // Edge Gallery parity: GPU ~10x faster; load() falls back to CPU on GPU-init failure
        val targetPath = bundle.file.absolutePath

        // M3: SERIALIZE with the foreground-service warm. The service is the PRIMARY
        // warmer ([preload] kicks it via LocalModelService.start). If it is
        // cold-loading THIS bundle, WAIT for it to pin the engine in the holder rather
        // than building a SECOND engine concurrently — two parallel 3.66GB GPU loads
        // OOM the device ("the model could not finish" / crash on launch). We give the
        // async service a brief head start to publish its [LocalEngineHolder.warmingPath]
        // marker, then keep waiting only while it is actively warming this exact bundle,
        // capped by a grace window. If it never claims the warm (service not running /
        // refused) we fall straight through to the unchanged USE_HOLDER/BUILD_OWN
        // decision below — the graceful fallback is preserved.
        run {
            val start = android.os.SystemClock.elapsedRealtime()
            while (LocalEngineHolder.getOrNull() == null &&
                android.os.SystemClock.elapsedRealtime() - start < SERVICE_WARM_GRACE_MS) {
                val serviceWarmingThis = LocalEngineHolder.warmingPath == targetPath
                val withinHeadStart =
                    android.os.SystemClock.elapsedRealtime() - start < SERVICE_WARM_HEADSTART_MS
                if (!serviceWarmingThis && !withinHeadStart) break
                delay(SERVICE_WARM_POLL_MS)
            }
        }

        // R2-C: PREFER the warm, PROCESS-resident engine pinned by
        // [LocalModelService] when it matches the active bundle -- it is already
        // loaded (no ~10-75s cold load) and survives VM/process recycles. The pure
        // [engineSourceFor] is the decision: USE_HOLDER iff the holder is non-empty
        // AND built for THIS bundle path; otherwise BUILD_OWN (the pre-R2-C path,
        // also taken when the service never started -- the graceful fallback).
        val held = LocalEngineHolder.getOrNull()
        if (engineSourceFor(held != null, LocalEngineHolder.modelPath, targetPath) == EngineSource.USE_HOLDER) {
            // Borrow the service-owned warm engine. We do NOT own it, so onCleared /
            // invalidate must not close() it (engineFromHolder = true).
            val warm = held!!
            engine = warm
            engineModelFile = bundle.file
            engineDelegate = delegate
            engineFromHolder = true
            // Reads the field so a later re-pin (loadWired) is picked up.
            val provider: () -> LocalLlm = { engine ?: warm }
            llmProvider = provider
            return provider
        }

        // BUILD_OWN: no warm engine to borrow (service not running / different
        // model). Build the VM-owned engine for the installed bundle (default CPU
        // delegate), threading the PER-MODEL config (Task W2): maxTokens (fallback
        // to the engine default when the descriptor leaves it null) + sampler trio.
        // This is exactly the pre-R2-C path -- the guaranteed fallback.
        val cfg = bundle.config
        val built = LiteRtEngine.fromInstalled(
            appContext,
            bundle.file,
            delegate = delegate,
            maxTokens = cfg.maxTokens ?: LiteRtEngine.DEFAULT_MAX_TOKENS,
            sampler = SamplerSettings(
                topK = cfg.topK,
                topP = cfg.topP,
                temperature = cfg.temperature,
            ),
            // Task W4: thread the per-model vision capability so the engine sets a
            // visionBackend + allows generateWithImage ONLY for image-capable bundles
            // (text-only models keep the working CPU text path untouched).
            supportImage = cfg.supportImage,
        )
        engine = built
        engineModelFile = bundle.file
        engineDelegate = delegate
        engineFromHolder = false
        // Reads the field: loadWired may adopt the engine another warmer
        // pinned for this bundle instead of loading this one.
        val provider: () -> LocalLlm = { engine ?: built }
        llmProvider = provider
        return provider
    }

    /**
     * Drop the cached on-device engine wiring (Task W5) so the next
     * [providerOrWire] / [preload] rebuilds it -- used when the
     * ACTIVE local model selection changes, so a different installed bundle is
     * actually loaded instead of the previously-wired one. Closes the old native
     * engine (idempotent, guarded) and clears the warm marker so a re-warm runs.
     */
    fun invalidate() {
        // R2-C: close ONLY a VM-OWNED engine; never the service-owned warm engine
        // borrowed from [LocalEngineHolder] (the service owns its lifecycle). We
        // just drop our reference + re-wire; if the active model changed, the
        // service's own re-warm (or the BUILD_OWN fallback) handles the new bundle.
        if (!engineFromHolder) {
            runCatching { engine?.close() }
        }
        engine = null
        engineModelFile = null
        engineFromHolder = false
        llmProvider = null
        warmingModelPath = null
        _state.value = LocalEngineState.IDLE
    }

    /**
     * I1 (W5 review): apply a model-selection change that arrived mid-turn. Called
     * at every local-turn settle point -- normal turn completion
     * ([ChatViewModel.runLocalEngineTurn]) AND, per the final-pass review, on STOP
     * ([ChatViewModel.cancelStream]) and CLEAR ([ChatViewModel.clearHistory]), since those cancel the stream
     * and so skip the completion path. A no-op unless [pendingReWarm] was set
     * (the consume decision is the pure [consumePendingReWarm]). Now that no
     * generation is in flight, it is safe to close() the old engine and re-warm
     * the newly-selected bundle.
     */
    fun processPendingReWarm() {
        // Consume the pending flag exactly once (pure decision, unit-tested).
        if (!ChatViewModel.consumePendingReWarm(pendingReWarm)) return
        pendingReWarm = false
        val closeHolder = pendingHolderClose
        pendingHolderClose = false
        if (!isLocalProvider()) {
            // No re-warm for a cloud provider, but a stale-config engine must still go.
            if (closeHolder) {
                invalidate()
                LocalEngineHolder.clearAndClose()
            }
            return
        }
        reWarm(closeHolder)
    }

    /**
     * Drop the current engine wiring and warm again (a model switch or a settings
     * apply). A preload warm still loading the OLD engine is joined first
     * (cancelAndJoin waits for its native load to return): building and loading the
     * new engine while the old load runs is the double-GPU-load OOM. [closeHolder] also
     * releases the holder's engine, whose window/sampler is now stale.
     */
    fun reWarm(closeHolder: Boolean = false) {
        val prior = warmJob
        scope.launch {
            prior?.cancelAndJoin()
            invalidate()
            if (closeHolder) LocalEngineHolder.clearAndClose()
            preload()
        }
    }

    /**
     * Task W1 — warm the on-device engine WHILE the app/chat is open, so the first
     * send is instant instead of paying the ~10-75s cold model load on the first
     * turn (matching Edge Gallery's "initialize once, keep warm" pattern).
     *
     * Gated + idempotent:
     *  - Only warms when the LOCAL provider is the ACTIVE/selected one — we do NOT
     *    pull a multi-GB model into RAM for a cloud-only session. DESIGN NOTE: an
     *    always-warm-on-open variant (for a future "another model calls the local
     *    engine as a tool" path) is a one-line change — drop the [isLocalProvider]
     *    `isLocal` gate below.
     *  - Resolves the production engine via [providerOrWire] (the SAME wiring
     *    the send path uses) on [Dispatchers.IO]; with no installed model it stays
     *    [LocalEngineState.IDLE] and does nothing.
     *  - Guards against duplicate/concurrent warms ([shouldStartWarm] + the
     *    [warmingModelPath] in-flight marker): a repeat trigger while WARMING, or
     *    while already READY for the same model, returns WITHOUT launching a second
     *    `load()` (which would just await the in-flight Mutex anyway) — no redundant
     *    coroutine, no state churn.
     *  - NEVER blocks Main and NEVER crashes: a load fault sets
     *    [LocalEngineState.ERROR] (the lazy fallback in [ChatViewModel.runLocalEngineTurn] still
     *    works — `load()` is idempotent — so the user can still send and the next
     *    trigger re-warms).
     *
     * Safe to call before [ChatViewModel.initialize] (no api → [providerOrWire] returns null
     * → IDLE) and to call repeatedly (the guards make repeats cheap no-ops).
     */
    fun preload() {
        // Provider gate: only warm for the active local provider (see DESIGN NOTE).
        if (!isLocalProvider()) return
        // Auto-warm SETTING gate (user preference): if the user opted OUT of
        // auto-warm-on-open, do NOT warm here -- leave the engine IDLE so the model
        // loads LAZILY on the first send (runLocalEngineTurn's load() is idempotent).
        // This is a user choice, checked BEFORE the OOM-crash guard; the send path
        // never consults it, so a deliberate send always warms.
        if (!localWarmPrefs.autoWarmEnabled()) {
            _state.value = LocalEngineState.IDLE
            Log.i(TAG, "skipping auto-warm: disabled by user setting; will warm lazily on first send")
            return
        }
        // Warm-loop guard (defense-in-depth): if the PREVIOUS warm is still marked
        // in-flight, the process was SIGKILLed mid-load (device OOM) and never ran
        // its success/failure cleanup. Auto-warming again would just OOM and crash
        // again -- an unbounded crash/restart loop. SKIP the auto-warm, RE-ARM the
        // flag (so a deliberate send can retry), and leave the engine IDLE (the UI
        // pill shows no spinner; a user send lazily warms). The send path does NOT
        // consult this flag, so the manual retry is always available.
        if (!com.aiblackbox.portal.data.local.WarmInflightStore.shouldAutoWarm(warmInflightStore.isInflight())) {
            warmInflightStore.setInflight(false)
            _state.value = LocalEngineState.IDLE
            Log.w(TAG, "skipping auto-warm: prior warm did not complete (likely OOM-killed); send to retry")
            return
        }
        // R2-C: the FOREGROUND SERVICE is now the PRIMARY warmer -- it pins the
        // engine in the PROCESS-level [LocalEngineHolder] so it loads ONCE and
        // survives VM/process recycles. Best-effort + idempotent (a second start
        // while a warm is in flight is ignored; LiteRtEngine.load() is itself
        // Mutex-idempotent). NEVER throws into us (LocalModelService.start swallows
        // a platform refusal), so the VM-side warm below stays the graceful
        // fallback: if the service no-ops, [providerOrWire] BUILDs its own
        // engine and this same coroutine load()s it, exactly as before R2-C.
        LocalModelService.start(appContext)
        // Cheap pre-launch guard: a warm is already in flight → nothing to do.
        if (!ChatViewModel.shouldStartWarm(_state.value)) return

        warmJob = scope.launch {
            // Resolve (and lazily wire) the production engine off the main thread.
            val provider = withContext(Dispatchers.IO) { providerOrWire() }
            if (provider == null) {
                // No installed model (or api not ready) → nothing to warm.
                _state.value = LocalEngineState.IDLE
                return@launch
            }
            val wired = engine
            val modelFile = engineModelFile
            // A FakeLocalLlm (tests) / fake-wired provider has no model file — there
            // is nothing to preload; reflect READY iff it reports already loaded.
            if (wired == null || modelFile == null) {
                _state.value =
                    if (provider().isLoaded) LocalEngineState.READY else LocalEngineState.IDLE
                return@launch
            }
            val targetPath = modelFile.absolutePath
            // Idempotency (model now known): already READY+loaded for this exact model
            // → keep the in-flight marker pinned and skip the redundant warm.
            if (_state.value == LocalEngineState.READY &&
                warmingModelPath == targetPath && wired.isLoaded) {
                return@launch
            }
            // Concurrency: another warm for this model is already mid-flight.
            if (warmingModelPath == targetPath && _state.value == LocalEngineState.WARMING) {
                return@launch
            }
            warmingModelPath = targetPath
            _state.value =
                ChatViewModel.localEngineStateAfter(_state.value, LocalEngineEvent.WARM_STARTED)
            try {
                // The one process warm: serialized with the service and control_phone
                // warms (instant when this bundle is already pinned), with the
                // crash-loop flag on disk for the whole load — an OOM-SIGKILL leaves it
                // set, so the next launch's preload SKIPS the auto-warm.
                loadWired()
                _state.value =
                    ChatViewModel.localEngineStateAfter(_state.value, LocalEngineEvent.WARM_SUCCEEDED)
                Log.d(TAG, "on-device engine warmed (READY) for $targetPath")
            } catch (e: kotlinx.coroutines.CancellationException) {
                // Cancellation (VM cleared / new warm) is NOT a fault — clear the
                // in-flight marker so a re-trigger can warm again, then unwind.
                if (warmingModelPath == targetPath) warmingModelPath = null
                throw e
            } catch (e: Exception) {
                // GRACEFUL (caught) failure: the next trigger retries the warm.
                _state.value =
                    ChatViewModel.localEngineStateAfter(_state.value, LocalEngineEvent.WARM_FAILED)
                // Clear the in-flight marker so the next trigger retries the warm.
                if (warmingModelPath == targetPath) warmingModelPath = null
                Log.w(TAG, "on-device engine warm failed (lazy fallback still works): ${e.message}")
            }
        }
    }

    /**
     * The persisted AUTO-WARM-on-open setting (default true). Read by the on-device
     * model settings screen to pre-fill its auto-warm Switch. A thin passthrough to
     * [LocalWarmPrefs] -- the single store the [preload] auto path reads.
     */
    fun autoWarmEnabled(): Boolean = localWarmPrefs.autoWarmEnabled()

    /**
     * Persist the AUTO-WARM-on-open setting from the settings screen Switch. When
     * false the [preload] auto path skips the warm and the model loads
     * lazily on the first send. Thin passthrough to [LocalWarmPrefs].
     */
    fun setAutoWarmEnabled(enabled: Boolean) = localWarmPrefs.setAutoWarmEnabled(enabled)

    /**
     * The ACTIVE on-device model's persisted [ModelConfig] (maxTokens + sampler
     * trio), used by the settings screen to PRE-FILL its window slider + sampler
     * fields with the user's current per-model choice. Reads the sidecar for
     * [currentModelSlug] (falling back to the first installed model, matching
     * [providerOrWire]'s selection) off the main thread. Returns an all-null
     * [ModelConfig] (engine defaults) when no model is installed or the api is not
     * ready, so callers can resolve each axis to its DEFAULT_* constant.
     */
    suspend fun currentModelConfig(): com.aiblackbox.portal.data.local.ModelConfig {
        val client = api() ?: return com.aiblackbox.portal.data.local.ModelConfig()
        return withContext(Dispatchers.IO) {
            val manager = LocalModelManager.fromContext(
                appContext, LocalModelApi(client), deviceId = "android-device",
            )
            val installed = runCatching { manager.installedModels() }.getOrDefault(emptyList())
            val bundle = installed.firstOrNull { it.slug == currentModelSlug }
                ?: installed.firstOrNull()
            bundle?.config ?: com.aiblackbox.portal.data.local.ModelConfig()
        }
    }

    /**
     * Apply a USER-changed on-device context window + sampler, then RE-WARM the
     * engine so the new values take effect (on-device settings apply layer; the
     * settings SCREEN is a later task -- this is the headless helper it calls).
     *
     * The on-device engine's `maxNumTokens`/`samplerConfig` are FIXED at the native
     * `initialize()`, so a live edit requires three steps, run off the main thread on
     * [Dispatchers.IO] (disk write + native teardown):
     *
     *  1. PERSIST via [LocalModelManager.updateModelConfig] for the ACTIVE
     *     [currentModelSlug] -- rewrites the `<slug>.json` sidecar so the next
     *     warm reads the new config (no-op + early-return if no model is installed,
     *     or the slug has no sidecar).
     *  2. DROP the stale-window engine: [invalidate] clears the VM
     *     wiring (closing a VM-OWNED engine; never the borrowed holder one), AND
     *     [LocalEngineHolder.clearAndClose] releases the PROCESS-held warm engine if
     *     the service pinned one -- otherwise the service-owned engine, still
     *     initialized at the OLD window, would be re-borrowed and the change lost.
     *  3. RE-WARM via [preload], which (via [LocalModelService.start] +
     *     [providerOrWire]) rebuilds + loads the engine at the NEW config and
     *     flips [state] to [LocalEngineState.WARMING] -> READY, so the UI
     *     can show a "reloading on-device model" state through the existing pill.
     *
     * Provider-gated like the warm path: a no-op when the active provider is not the
     * local one (we never pull a multi-GB model into RAM for a cloud-only session).
     * Pass null for any axis to leave it unchanged (see [mergedConfig]).
     *
     * @param maxTokens new context window, or null to keep the current value.
     * @param topK / topP / temperature sampler overrides, null to keep current.
     */
    fun applySettings(
        maxTokens: Int?,
        topK: Int?,
        topP: Float?,
        temperature: Float?,
    ) {
        // Provider gate: only re-warm for the active local provider (mirrors
        // [preload]); a cloud-only session has no engine to reconfigure.
        if (!isLocalProvider()) return
        val slug = currentModelSlug
        if (slug.isBlank()) {
            Log.w(TAG, "applySettings: no active on-device model slug; nothing to persist")
            return
        }
        val client = api() ?: return
        val sampler = SamplerSettings(topK = topK, topP = topP, temperature = temperature)
        scope.launch {
            // (1) Persist the new config off the main thread (disk write).
            val persisted = withContext(Dispatchers.IO) {
                val manager = LocalModelManager.fromContext(
                    appContext, LocalModelApi(client), deviceId = "android-device",
                )
                runCatching { manager.updateModelConfig(slug, maxTokens, sampler) }
                    .getOrDefault(false)
            }
            if (!persisted) {
                // Slug not installed / unreadable sidecar -> nothing to re-warm.
                Log.w(TAG, "applySettings: no sidecar updated for '$slug'; skipping re-warm")
                return@launch
            }
            // (2) + (3) Drop BOTH the VM wiring AND the process-held warm engine so the
            // stale-window engine is not reused, then re-warm at the NEW config (the
            // pill shows WARMING -> READY). While an on-device reply is still
            // generating, closing the engine under it is the mid-generation teardown
            // the I1 design forbids (A04-chat-viewmodel-08): defer both to the turn's
            // settle point, exactly like a mid-turn model switch.
            if (turnInFlight()) {
                pendingHolderClose = true
                pendingReWarm = true
                return@launch
            }
            reWarm(closeHolder = true)
        }
    }

    companion object {
        // M3 serialization ([providerOrWire] waits for the foreground service's
        // SINGLE warm rather than racing it with a 2nd concurrent engine build — the
        // double-warm that OOM'd the device on launch). HEADSTART covers the async gap
        // between LocalModelService.start() and the service publishing its warming
        // marker; GRACE caps the wait for a slow GPU cold load; POLL is the check tick.
        private const val SERVICE_WARM_HEADSTART_MS = 2_500L
        private const val SERVICE_WARM_GRACE_MS = 90_000L
        private const val SERVICE_WARM_POLL_MS = 200L
    }
}
