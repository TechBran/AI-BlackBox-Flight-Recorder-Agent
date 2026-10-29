package com.aiblackbox.portal.data.local

import android.content.Context
import android.util.Log
import com.aiblackbox.portal.data.api.LocalModelDownloader
import com.aiblackbox.portal.data.model.AttestRequest
import com.aiblackbox.portal.data.model.LocalBundle
import com.aiblackbox.portal.data.store.BlackBoxStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.isActive
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import java.io.File

/** No-op downloader for the disk-only installed-models scan (never hits the network). */
private object WarmNoopDownloader : LocalModelDownloader {
    override suspend fun download(
        bundle: LocalBundle,
        destFile: File,
        onProgress: (bytesSoFar: Long, totalBytes: Long) -> Unit,
    ): Result<File> = Result.failure(UnsupportedOperationException("disk scan only"))

    override suspend fun attest(req: AttestRequest): Boolean = false
}

/** The DataStore key the Model Manager persists the active on-device slug under. */
private const val PREF_ACTIVE_LOCAL_MODEL = "model_local"

/**
 * What [pinWarm] needs from the process holder. A seam so the warm protocol is
 * JVM-testable with a fake engine; production is [HolderSlot] over [LocalEngineHolder].
 */
interface EngineSlot<E : Any> {
    fun held(): E?
    fun heldPath(): String?

    /** Release the held engine (its native close waits for any running generation). */
    fun retire()
    fun beginWarming(path: String)
    fun endWarming(path: String)
    fun pin(engine: E, path: String)
}

/**
 * The ONE warm protocol (A09-data-local-model-02, A04-chat-viewmodel-09). Every engine
 * load in the process runs it under the same [mutex], so two multi-GB GPU loads can
 * never overlap (the double-warm OOM). Under the lock it:
 *  - returns the engine already pinned for [path] when [sameConfig] says it was built
 *    like [engine] (context window, sampler), closing the unloaded [engine] it was
 *    handed, so a second warmer reuses the first one's work instead of loading a copy;
 *  - retires an engine pinned for a DIFFERENT model, or for the same model with a
 *    config that has since changed, before loading, so two engines never sit in memory
 *    together and a settings apply is never answered with the stale engine;
 *  - publishes the warm marker ([EngineSlot.beginWarming]) that the chat waits on, and
 *    the crash-loop flag ([WarmInflightStore]) that a SIGKILL mid-load leaves set;
 *  - pins the loaded engine in the slot (the single owner), or closes it and rethrows
 *    when the load fails.
 *
 * The native load cannot be interrupted, so it always runs to its end. A caller
 * cancelled meanwhile (a STOP, an operator switch) still gets the loaded engine pinned
 * when [keepIfCancelled] says the engine is still wanted, and the cancellation then
 * surfaces at its next suspension; otherwise (a service stopping, a cleared screen) the
 * engine is closed and the cancellation rethrown, so nothing stays pinned unowned.
 */
suspend fun <E : Any> pinWarm(
    mutex: Mutex,
    slot: EngineSlot<E>,
    inflight: WarmInflightStore,
    engine: E,
    path: String,
    load: suspend (E) -> Unit,
    close: (E) -> Unit,
    sameConfig: (held: E, wanted: E) -> Boolean = { _, _ -> true },
    keepIfCancelled: () -> Boolean = { false },
): E = mutex.withLock {
    val held = slot.held()
    if (held != null && slot.heldPath() == path && (held === engine || sameConfig(held, engine))) {
        if (held !== engine) runCatching { close(engine) }
        return@withLock held
    }
    if (held != null) slot.retire()
    slot.beginWarming(path)
    inflight.setInflight(true)
    try {
        // Only a load that THREW closes the engine; a cancelled caller is decided below.
        withContext(NonCancellable) {
            try {
                load(engine)
            } catch (t: Throwable) {
                runCatching { close(engine) }
                throw t
            }
        }
        if (!currentCoroutineContext().isActive && !keepIfCancelled()) {
            runCatching { close(engine) }
            currentCoroutineContext().ensureActive()
        }
        slot.pin(engine, path)
        engine
    } finally {
        // Success, a caught failure and a cancellation are all graceful: only a real
        // process kill leaves the flag set for the next launch's auto-warm to skip on.
        runCatching { inflight.setInflight(false) }
        slot.endWarming(path)
    }
}

/** [EngineSlot] over the process-level [LocalEngineHolder]. */
private class HolderSlot(private val delegate: String) : EngineSlot<LiteRtEngine> {
    override fun held(): LiteRtEngine? = LocalEngineHolder.getOrNull()
    override fun heldPath(): String? = LocalEngineHolder.modelPath
    override fun retire() = LocalEngineHolder.clearAndClose()
    override fun beginWarming(path: String) = LocalEngineHolder.beginWarming(path)
    override fun endWarming(path: String) = LocalEngineHolder.endWarming(path)
    override fun pin(engine: LiteRtEngine, path: String) = LocalEngineHolder.set(engine, path, delegate)
}

/**
 * Load [engine] (built for the bundle at [path]) through the one process warm and
 * return the engine to use: [engine] itself once pinned, or the engine another warmer
 * already pinned for [path] with the same config ([LiteRtEngine.configKey]). The
 * foreground service's warm, the control_phone wake and the chat's own load all go
 * through here, so the engine has exactly one owner ([LocalEngineHolder]) and is loaded
 * once. [keepIfCancelled]: see [pinWarm].
 */
suspend fun pinWarmEngine(
    context: Context,
    engine: LiteRtEngine,
    path: String,
    delegate: String,
    keepIfCancelled: () -> Boolean = { false },
): LiteRtEngine = pinWarm(
    mutex = LocalEngineHolder.warmMutex,
    slot = HolderSlot(delegate),
    inflight = WarmInflightStore.fromContext(context),
    engine = engine,
    path = path,
    load = { withContext(Dispatchers.IO) { it.load(File(path), delegate) } },
    close = { it.close() },
    sameConfig = { held, wanted -> held.configKey == wanted.configKey },
    keepIfCancelled = keepIfCancelled,
)

/**
 * Resolve the ACTIVE installed bundle (the `model_local` slug, else the
 * alphabetically-first installed one) and build — NOT load — its engine. Returns
 * (engine, absolute bundle path, delegate), or null when no model is installed. A
 * hermetic disk scan: the downloader is a no-op and never reached.
 */
suspend fun resolveActiveEngine(appContext: Context): Triple<LiteRtEngine, String, String>? =
    withContext(Dispatchers.IO) {
        val activeSlug = runCatching {
            BlackBoxStore(appContext).getString(PREF_ACTIVE_LOCAL_MODEL).first()
        }.getOrDefault("")
        val manager = LocalModelManager.fromContext(appContext, WarmNoopDownloader, deviceId = "android-device")
        val installed = runCatching { manager.installedModels() }.getOrDefault(emptyList())
        val bundle = installed.firstOrNull { it.slug == activeSlug }
            ?: installed.firstOrNull()
            ?: return@withContext null
        val cfg = bundle.config
        // GPU (Edge Gallery parity, ~10x faster than CPU); LiteRtEngine.load falls back
        // to CPU if GPU init fails on a limited device.
        val delegate = "gpu"
        val engine = LiteRtEngine.fromInstalled(
            appContext,
            bundle.file,
            delegate = delegate,
            maxTokens = cfg.maxTokens ?: LiteRtEngine.DEFAULT_MAX_TOKENS,
            sampler = SamplerSettings(
                topK = cfg.topK,
                topP = cfg.topP,
                temperature = cfg.temperature,
            ),
            supportImage = cfg.supportImage,
        )
        Triple(engine, bundle.file.absolutePath, delegate)
    }

/**
 * Ensure a WARM on-device engine is pinned in [LocalEngineHolder], LOADING it on demand
 * when the holder is empty — the "wake Gemma on your phone" path for control_phone, which
 * must NOT depend on whether [com.aiblackbox.portal.LocalModelService] happened to
 * pre-warm (the chat path warms lazily on first message, so a remote task that arrives
 * before any chat would otherwise find a cold engine and fail instantly).
 *
 * The load goes through [pinWarmEngine], the same single-flight warm the service and the
 * chat use: a wake that arrives while either is cold-loading waits for that load and
 * borrows its engine instead of starting a second ~3.66GB GPU load, and a SIGKILL during
 * the wake leaves the crash-loop flag set like any other warm.
 *
 * Returns the warm engine (a [NativeToolCallingLlm]) or null when no model is installed /
 * the load fails. Idempotent: an already-warm holder engine is returned immediately.
 * Best-effort: never throws (returns null on any failure); cancellation propagates.
 */
suspend fun ensureWarmEngine(appContext: Context): NativeToolCallingLlm? {
    (LocalEngineHolder.getOrNull() as? NativeToolCallingLlm)?.let { return it }
    return try {
        val (engine, path, delegate) = resolveActiveEngine(appContext) ?: return null
        pinWarmEngine(appContext, engine, path, delegate)   // the ~10-75s cold load (the "waking")
    } catch (e: kotlinx.coroutines.CancellationException) {
        throw e
    } catch (e: Exception) {
        Log.w("EngineWarm", "on-demand warm failed (${e.javaClass.simpleName})")
        null
    }
}
