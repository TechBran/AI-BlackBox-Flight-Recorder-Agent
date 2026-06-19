package com.aiblackbox.portal.data.local

import android.content.Context
import android.util.Log
import com.aiblackbox.portal.data.api.LocalModelDownloader
import com.aiblackbox.portal.data.model.AttestRequest
import com.aiblackbox.portal.data.store.BlackBoxStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.withContext
import java.io.File

/** No-op downloader for the disk-only installed-models scan (never hits the network). */
private object WarmNoopDownloader : LocalModelDownloader {
    override suspend fun download(
        slug: String,
        destFile: File,
        onProgress: (bytesSoFar: Long, totalBytes: Long) -> Unit,
    ): Result<File> = Result.failure(UnsupportedOperationException("disk scan only"))

    override suspend fun attest(req: AttestRequest): Boolean = false
}

/**
 * Ensure a WARM on-device engine is pinned in [LocalEngineHolder], LOADING it on demand
 * when the holder is empty — the "wake Gemma on your phone" path for control_phone, which
 * must NOT depend on whether [com.aiblackbox.portal.LocalModelService] happened to
 * pre-warm (the chat path warms lazily on first message, so a remote task that arrives
 * before any chat would otherwise find a cold engine and fail instantly).
 *
 * Returns the warm engine (a [NativeToolCallingLlm]) or null when no model is installed /
 * the load fails. Idempotent: an already-warm holder engine is returned immediately.
 * Mirrors [com.aiblackbox.portal.LocalModelService]'s resolveActiveBundle + load +
 * holder.set. Best-effort: never throws (returns null on any failure).
 */
suspend fun ensureWarmEngine(appContext: Context): NativeToolCallingLlm? {
    (LocalEngineHolder.getOrNull() as? NativeToolCallingLlm)?.let { return it }
    return try {
        withContext(Dispatchers.IO) {
            val activeSlug = runCatching {
                BlackBoxStore(appContext).getString("model_local").first()
            }.getOrDefault("")
            val manager = LocalModelManager.fromContext(appContext, WarmNoopDownloader, deviceId = "android-device")
            val installed = runCatching { manager.installedModels() }.getOrDefault(emptyList())
            val bundle = installed.firstOrNull { it.slug == activeSlug }
                ?: installed.firstOrNull()
                ?: return@withContext null
            val cfg = bundle.config
            val delegate = "cpu"
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
            engine.load(bundle.file, delegate)   // the ~10-75s cold load (the "waking")
            LocalEngineHolder.set(engine, bundle.file.absolutePath, delegate)
            engine
        }
    } catch (e: Exception) {
        Log.w("EngineWarm", "on-demand warm failed (${e.javaClass.simpleName})")
        null
    }
}
