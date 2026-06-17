package com.aiblackbox.portal.data.local

/**
 * Process-level holder for the warm on-device [LiteRtEngine] (Task R2-C).
 *
 * **Why this exists.** The on-device model's cold load is ~10-75s. Before R2-C the
 * engine was a SINGLETON owned by [com.aiblackbox.portal.ui.chat.ChatViewModel]
 * ([com.aiblackbox.portal.ui.chat.ChatViewModel.localEngine]) and closed in
 * `onCleared`, so it died whenever the ViewModel / process was reclaimed -- every
 * fresh VM paid the cold load again. This holder lifts the warm engine to the
 * PROCESS level so it survives a VM teardown, and [LocalModelService] (a foreground
 * service) keeps the process alive so Android does not reclaim it while backgrounded.
 *
 * **Ownership.** The engine here is owned by [LocalModelService]: the service
 * [set]s it after a warm load and [clearAndClose]s it on stop/destroy. A consumer
 * ([com.aiblackbox.portal.ui.chat.ChatViewModel]) only READS it via [getOrNull] and
 * must NEVER close it (it does not own it) -- closing the held engine out from under
 * the service would break a future turn. The ViewModel's own fallback engine (built
 * when this holder is empty / mismatched -- see [engineSourceFor]) is VM-owned and
 * still closed in `onCleared`, exactly as before.
 *
 * **Graceful fallback (the R2-C safety guarantee).** This whole path is ADDITIVE:
 * if the service never starts, the holder stays empty and [getOrNull] returns null,
 * so the consumer builds + uses its OWN engine exactly as it did before R2-C. The
 * worst case of any holder/service failure is therefore "no startup-latency win",
 * NEVER a broken chat.
 *
 * **Identity.** The held engine is built for ONE installed bundle on ONE delegate.
 * [modelPath] / [delegate] record which, so a consumer can tell (via [engineSourceFor])
 * whether the held engine matches the ACTIVE model before reusing it -- if the user
 * switched models, the held engine is the wrong one and the consumer builds its own.
 *
 * No Android dependencies beyond the [LiteRtEngine] it holds, so the get/set/clear
 * surface and the pure [engineSourceFor] decision are JVM-unit-testable.
 */
object LocalEngineHolder {

    // The process-resident warm engine, or null when nothing is held. @Volatile so a
    // consumer thread sees the service thread's set()/clearAndClose() without locking.
    @Volatile
    private var engine: LiteRtEngine? = null

    // Absolute model-bundle path + delegate the held [engine] was built for, so a
    // consumer can match it against the active model ([engineSourceFor]). Written
    // together with [engine] under [set] / cleared together under [clearAndClose].
    @Volatile
    var modelPath: String? = null
        private set

    @Volatile
    var delegate: String? = null
        private set

    /** The held warm engine, or null when nothing is held (consumer falls back). */
    fun getOrNull(): LiteRtEngine? = engine

    /**
     * Store [engine] as the process-resident warm engine, recording the [modelPath]
     * (absolute bundle path) + [delegate] it was built for. If a DIFFERENT engine is
     * already held it is closed first (so we never leak a native engine on a model
     * switch). Idempotent for the same instance (re-[set]ting the held engine just
     * refreshes the identity, never closes the live engine).
     *
     * Called by [LocalModelService] after a successful warm load.
     */
    @Synchronized
    fun set(engine: LiteRtEngine, modelPath: String, delegate: String) {
        val prior = this.engine
        if (prior != null && prior !== engine) {
            runCatching { prior.close() }
        }
        this.engine = engine
        this.modelPath = modelPath
        this.delegate = delegate
    }

    /**
     * Release + forget the held engine (idempotent; safe when nothing is held).
     * Called by [LocalModelService] on stop/destroy. The native [LiteRtEngine.close]
     * is guarded so a teardown throw can't crash the service.
     */
    @Synchronized
    fun clearAndClose() {
        runCatching { engine?.close() }
        engine = null
        modelPath = null
        delegate = null
    }
}

/**
 * Where the on-device turn should get its engine (Task R2-C). PURE (primitives only)
 * so the "use the warm process-held engine IFF it matches the active model, else
 * build my own" decision is JVM-unit-testable under JDK 17 without a real engine /
 * the AndroidViewModel. (It takes [holderHasEngine] rather than a [LiteRtEngine?]
 * because constructing the litertlm-backed engine on the host test JVM throws
 * UnsupportedClassVersionError -- see the Mappers header in LiteRtEngine.kt.)
 *
 * [com.aiblackbox.portal.ui.chat.ChatViewModel.localProviderOrWire] applies exactly
 * this: it prefers the warm [LocalEngineHolder] engine when present AND built for the
 * active model's bundle path; otherwise it builds (and owns) its own engine -- the
 * pre-R2-C fallback, which is also the path taken when the service never started.
 *
 *  - [holderHasEngine] false (service not running / holder empty) -> [EngineSource.BUILD_OWN].
 *  - held but [holderModelPath] != [activeModelPath] (user switched models) ->
 *    [EngineSource.BUILD_OWN] (the held engine is the wrong bundle).
 *  - held AND the path matches -> [EngineSource.USE_HOLDER] (warm, instant).
 *
 * A blank/empty [activeModelPath] can never match a held path, so it falls back to
 * BUILD_OWN (defensive -- the caller always resolves a concrete path first). The
 * call site passes `LocalEngineHolder.getOrNull() != null` for [holderHasEngine].
 */
fun engineSourceFor(
    holderHasEngine: Boolean,
    holderModelPath: String?,
    activeModelPath: String,
): EngineSource = when {
    !holderHasEngine -> EngineSource.BUILD_OWN
    activeModelPath.isBlank() -> EngineSource.BUILD_OWN
    holderModelPath == activeModelPath -> EngineSource.USE_HOLDER
    else -> EngineSource.BUILD_OWN
}

/** The two engine sources for an on-device turn (see [engineSourceFor]). */
enum class EngineSource {
    /** Reuse the warm process-held engine ([LocalEngineHolder]) -- matches the active model. */
    USE_HOLDER,

    /** Build (and own) a VM-local engine -- the pre-R2-C path / graceful fallback. */
    BUILD_OWN,
}
