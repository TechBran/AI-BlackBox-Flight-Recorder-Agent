package com.aiblackbox.portal.ui.chat

import android.content.Context
import android.media.MediaPlayer
import android.util.Log
import android.widget.Toast
import com.aiblackbox.portal.BackgroundTaskService
import com.aiblackbox.portal.buildTtsBatchBody
import com.aiblackbox.portal.data.api.ApiHttpException
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.repository.TtsQueue
import com.aiblackbox.portal.data.repository.TtsQueueFailedException
import com.aiblackbox.portal.data.repository.TtsQueueUnavailableException
import com.aiblackbox.portal.data.repository.TtsRepository
import com.aiblackbox.portal.data.wellness.WellnessChatRules
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.File

/**
 * Chat's spoken replies, out of NativeMainActivity's setContent (audit A01-activities-root-10 /
 * NEW-android-1, rank 76): the auto-TTS of a finished reply, the speaker on a message bubble,
 * and plain speech with no bubble. Each reads the operator's voice, sends an on-box voice
 * through the async queue when the box has one (B3), and otherwise asks /tts/batch. Moved
 * verbatim; the bubble state goes through [Bubbles].
 *
 * (M7) A reply that used the person's health data is never read aloud, with any voice
 * ([WellnessChatRules.maySpeak]): a cloud voice is a service the health settings never allowed,
 * and the on-box voice writes its clip under Portal/uploads. Auto-TTS skips it; the speaker
 * says why. The box refuses such a text on its own (403 wellness_text): every call here then
 * shows the same words and never retries.
 */
internal class ChatSpeech(
    /** The application context: the cache dir the clips go to, toasts, the keep-alive service. */
    private val context: Context,
    /** Where the speaker and plain speech run (the composition's scope). */
    private val scope: CoroutineScope,
    private val api: () -> BlackBoxApi?,
    /** The current operator, read each time it is needed. */
    private val operatorOf: () -> String,
    /** The operator's chosen voice ("provider:voice", "" for the box default). */
    private val voiceFor: suspend (String) -> String,
    private val bubbles: Bubbles,
    /** Plays a finished clip once and deletes it; MediaPlayer by default. */
    private val play: (File) -> Unit = ::playOnceThenDelete,
) {
    /** A message bubble's speech state (ChatViewModel's in the app). */
    interface Bubbles {
        /** The speaker spins ([generating]) with an optional status chip, or goes idle. */
        fun setGenerating(messageId: String, generating: Boolean, status: String?)
        /** The clip is ready: the bubble's player appears and the spinner stops. */
        fun setAudioPath(messageId: String, path: String)
        /** Did message [messageId]'s turn use the person's health data (its wellness_used)? */
        fun usedHealth(messageId: String): Boolean = false
    }

    private val operator: String get() = operatorOf()

    /** The box refused the text as the operator's health data (403 wellness_text; [WellnessChatRules.isHealthTextRefusal]). */
    private fun isHealthRefusal(e: Throwable): Boolean =
        (e as? ApiHttpException)?.let { WellnessChatRules.isHealthTextRefusal(it.code, it.error) } == true

    /** The words for a health reply that is not spoken; the same on the Portal. */
    private suspend fun sayNotSpoken() = withContext(Dispatchers.Main) {
        Toast.makeText(context, WellnessChatRules.NOT_SPOKEN_MESSAGE, Toast.LENGTH_LONG).show()
    }

    /** A direct /tts/batch answer that is the box's health refusal (its body is read, and closed). */
    private fun isHealthRefusal(response: okhttp3.Response): Boolean =
        response.code == 403 && WellnessChatRules.isHealthTextRefusal(403, runCatching { response.body?.bytes() }.getOrNull())

    // =========================================================================
    // B3 (2026-07-22): on-box (qwen) TTS via the async B1 server queue.
    // Submit (POST /tts/queue) -> poll (GET /tts/task/{id}, 1.5s cadence) ->
    // download the finished WAV. Poll requests are SHORT, so this path no
    // longer leans on the 300s ttsClient read-timeout stopgap. Cloud
    // providers never come through here — their direct paths are untouched.
    // =========================================================================

    /**
     * Queue an on-box clip for a message bubble, driving the live status chip
     * via setGenerating(id, true, "Queued — N ahead" / "Generating M/K…").
     *
     * Returns true when HANDLED — audio delivered (player appears) or a
     * terminal failure was toasted (speaker resets to idle = the retry
     * affordance) — so the caller must NOT run the direct /tts/batch path.
     * Returns false ONLY when the backend has no /tts/queue (404, pre-B1
     * build): the caller falls back to the existing direct call, byte-for-byte
     * unchanged.
     */
    private suspend fun tryQueueTtsForMessage(
        api: BlackBoxApi,
        messageId: String,
        text: String,
        voice: String,
        operator: String,
    ): Boolean {
        bubbles.setGenerating(messageId, true, "Queueing audio…")
        return try {
            val bytes = TtsRepository(api).generateViaQueue(
                text = text, voice = voice, operator = operator,
            ) { st ->
                bubbles.setGenerating(messageId, true, TtsQueue.statusLine(st))
            }
            val tempFile = File(context.cacheDir, "tts_q_${messageId.take(8)}_${System.currentTimeMillis()}.wav")
            withContext(Dispatchers.IO) { tempFile.writeBytes(bytes) }
            // Sets the audio path AND clears ttsGenerating + the status chip.
            bubbles.setAudioPath(messageId, tempFile.absolutePath)
            true
        } catch (e: TtsQueueUnavailableException) {
            // Pre-B1 backend: clear the chip but KEEP the spinner — the caller
            // continues straight into the direct /tts/batch path.
            bubbles.setGenerating(messageId, true, null)
            false
        } catch (e: kotlinx.coroutines.CancellationException) {
            bubbles.setGenerating(messageId, false, null)
            throw e
        } catch (e: Exception) {
            if (isHealthRefusal(e)) {
                // The box will not speak this text (it recalls health data): no retry, no fallback.
                bubbles.setGenerating(messageId, false, null)
                sayNotSpoken()
                return true
            }
            Log.e("TTS", "Queue TTS failed: ${e.message}", e)
            // Reset to idle — tapping the speaker again IS the retry action.
            bubbles.setGenerating(messageId, false, null)
            val retryable = (e as? TtsQueueFailedException)?.retryable ?: true
            val hint = if (retryable) " — tap the speaker to retry" else ""
            withContext(Dispatchers.Main) {
                Toast.makeText(
                    context,
                    "Audio failed: ${e.message?.take(80) ?: "unknown error"}$hint",
                    Toast.LENGTH_LONG
                ).show()
            }
            true
        }
    }

    /**
     * Auto-TTS: speak a finished reply into its bubble ([messageId], captured before the
     * request). Mirrors the speaker flow: spinner, generate, player. Runs to the end before
     * the next reply is taken.
     */
    suspend fun autoSpeak(messageId: String, text: String) {
        // Show generating spinner (same as manual TTS)
        bubbles.setGenerating(messageId, true, null)

        try {
            val api = api()
            if (api == null) {
                bubbles.setGenerating(messageId, false, null)
                return
            }
            // M7: a health reply is never read aloud, by any voice (the box refuses it too).
            if (!WellnessChatRules.maySpeak(bubbles.usedHealth(messageId))) {
                bubbles.setGenerating(messageId, false, null)
                return
            }
            val voiceValue = voiceFor(operator)
            val config = TtsRepository.parseVoice(voiceValue)
            // B3: on-box voices go through the async queue when the
            // backend has it (404 -> direct /tts/batch fallback below).
            if (config.provider == TtsRepository.ON_BOX_PROVIDER &&
                tryQueueTtsForMessage(api, messageId, text, config.voice, operator)
            ) {
                return
            }
            val body = buildTtsBatchBody(text, config.voice, "mp3", config.provider, operator)
            val request = okhttp3.Request.Builder()
                .url("${api.getBaseUrl()}/tts/batch")
                .post(body.toRequestBody("application/json".toMediaType()))
                .build()
            val response = withContext(Dispatchers.IO) {
                api.getTtsClient().newCall(request).execute()
            }
            if (response.isSuccessful) {
                val bytes = withContext(Dispatchers.IO) { response.body?.bytes() }
                if (bytes != null && bytes.isNotEmpty()) {
                    val ext = if (config.provider == "gemini-pro" || config.provider == "gemini-flash") "wav" else "mp3"
                    val tempFile = File(context.cacheDir, "auto_tts_${System.currentTimeMillis()}.$ext")
                    withContext(Dispatchers.IO) { tempFile.writeBytes(bytes) }
                    // Sets audio URL AND resets ttsGenerating = false
                    bubbles.setAudioPath(messageId, tempFile.absolutePath)
                } else {
                    bubbles.setGenerating(messageId, false, null)
                }
            } else {
                val healthText = withContext(Dispatchers.IO) { isHealthRefusal(response) }
                bubbles.setGenerating(messageId, false, null)
                if (healthText) sayNotSpoken()
            }
        } catch (e: Exception) {
            Log.e("AutoTTS", "Failed: ${e.message}", e)
            bubbles.setGenerating(messageId, false, null)
        }
    }

    /** Speak [text] with no bubble: the clip plays once and is deleted. */
    fun speak(text: String) {
        scope.launch {
            try {
                val api = api() ?: return@launch
                val voiceValue = voiceFor(operator)
                val config = TtsRepository.parseVoice(voiceValue)
                // B3: on-box voices go through the async queue when the
                // backend has it (404 -> direct /tts/batch fallback below).
                // No message bubble here, so no status chip — but the poll
                // path survives arbitrarily long queue waits.
                if (config.provider == TtsRepository.ON_BOX_PROVIDER) {
                    val queued: ByteArray? = try {
                        TtsRepository(api).generateViaQueue(text, config.voice, operator)
                    } catch (e: TtsQueueUnavailableException) {
                        null // pre-B1 backend — fall through to /tts/batch
                    } catch (e: TtsQueueFailedException) {
                        Toast.makeText(
                            context,
                            "Audio failed: ${e.message?.take(80)}",
                            Toast.LENGTH_LONG
                        ).show()
                        return@launch
                    } catch (e: ApiHttpException) {
                        // M7: the box will not speak a text that recalls health data; no fallback.
                        if (!isHealthRefusal(e)) throw e
                        sayNotSpoken()
                        return@launch
                    }
                    if (queued != null) {
                        val tempFile = File(context.cacheDir, "tts_${System.currentTimeMillis()}.wav")
                        withContext(Dispatchers.IO) { tempFile.writeBytes(queued) }
                        play(tempFile)
                        return@launch
                    }
                }
                val body = buildTtsBatchBody(text, config.voice, "mp3", config.provider, operator)
                val request = okhttp3.Request.Builder()
                    .url("${api.getBaseUrl()}/tts/batch")
                    .post(body.toRequestBody("application/json".toMediaType()))
                    .build()
                val response = withContext(Dispatchers.IO) {
                    api.getTtsClient().newCall(request).execute()
                }
                if (response.isSuccessful) {
                    val bytes = withContext(Dispatchers.IO) { response.body?.bytes() }
                    if (bytes != null && bytes.isNotEmpty()) {
                        val tempFile = File(context.cacheDir, "tts_${System.currentTimeMillis()}.mp3")
                        tempFile.writeBytes(bytes)
                        play(tempFile)
                    }
                } else if (withContext(Dispatchers.IO) { isHealthRefusal(response) }) {
                    sayNotSpoken()
                }
            } catch (e: Exception) {
                Log.e("TTS", "Playback failed: ${e.message}", e)
            }
        }
    }

    /**
     * A bubble's speaker: generate [text] into message [messageId]'s player. A foreground
     * service keeps the app alive while it generates; any failure resets the speaker so a
     * second tap retries.
     */
    fun speakForMessage(messageId: String, text: String) {
        // M7: a health reply is never read aloud, by any voice; say why and send nothing.
        if (!WellnessChatRules.maySpeak(bubbles.usedHealth(messageId))) {
            Toast.makeText(context, WellnessChatRules.NOT_SPOKEN_MESSAGE, Toast.LENGTH_LONG).show()
            return
        }
        // Mark as generating (button turns red)
        bubbles.setGenerating(messageId, true, null)
        Toast.makeText(context, "Generating speech...", Toast.LENGTH_SHORT).show()
        // Keep alive in background for TTS generation
        try {
            val svcIntent = android.content.Intent(context, BackgroundTaskService::class.java).apply {
                action = BackgroundTaskService.ACTION_START
                putExtra(BackgroundTaskService.EXTRA_TASK_LABEL, "Generating speech...")
            }
            if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
                context.startForegroundService(svcIntent)
            } else {
                context.startService(svcIntent)
            }
        } catch (_: Exception) {}
        scope.launch {
            // D10 (Task 7.9): declared outside the try so the finally block
            // can always cancel the slow-first-byte watchdog.
            var slowToastJob: kotlinx.coroutines.Job? = null
            try {
                val api = api()
                if (api == null) {
                    bubbles.setGenerating(messageId, false, null)
                    return@launch
                }
                val voiceValue = voiceFor(operator)
                val config = TtsRepository.parseVoice(voiceValue)
                // B3: on-box voices go through the async queue when the
                // backend has it — live chip on the bubble, short poll
                // requests. 404 (pre-B1 backend) returns false and falls
                // through to the direct path below; the outer finally still
                // stops the background service on every exit.
                if (config.provider == TtsRepository.ON_BOX_PROVIDER &&
                    tryQueueTtsForMessage(api, messageId, text, config.voice, operator)
                ) {
                    return@launch
                }
                // D10 (Task 7.9): on-box (qwen) voices may wait on a GPU group
                // swap; show "loading models…" only if the first byte is slow.
                slowToastJob = if (config.provider == TtsRepository.ON_BOX_PROVIDER) scope.launch {
                    TtsRepository.awaitSlowFirstByte(
                        stillInFlight = { true },
                        onSlow = { Toast.makeText(context, "Loading on-box voice models…", Toast.LENGTH_SHORT).show() },
                    )
                } else null
                val body = buildTtsBatchBody(text, config.voice, "mp3", config.provider, operator)
                val request = okhttp3.Request.Builder()
                    .url("${api.getBaseUrl()}/tts/batch")
                    .post(body.toRequestBody("application/json".toMediaType()))
                    .build()
                val response = withContext(Dispatchers.IO) {
                    api.getTtsClient().newCall(request).execute()
                }
                if (response.isSuccessful) {
                    val bytes = withContext(Dispatchers.IO) { response.body?.bytes() }
                    if (bytes != null && bytes.isNotEmpty()) {
                        val ext = if (config.provider == "gemini-pro" || config.provider == "gemini-flash") "wav" else "mp3"
                        val tempFile = File(context.cacheDir, "tts_${messageId.take(8)}_${System.currentTimeMillis()}.$ext")
                        withContext(Dispatchers.IO) { tempFile.writeBytes(bytes) }
                        // Store file path — button turns green, AudioPlayerBar appears
                        bubbles.setAudioPath(messageId, tempFile.absolutePath)
                    } else {
                        // 200 but empty body — reset so button is clickable again
                        bubbles.setGenerating(messageId, false, null)
                        withContext(Dispatchers.Main) {
                            Toast.makeText(context, "TTS: empty response", Toast.LENGTH_SHORT).show()
                        }
                    }
                } else {
                    val healthText = withContext(Dispatchers.IO) { isHealthRefusal(response) }
                    bubbles.setGenerating(messageId, false, null)
                    if (healthText) sayNotSpoken()
                    else withContext(Dispatchers.Main) {
                        Toast.makeText(context, "TTS failed: ${response.code}", Toast.LENGTH_SHORT).show()
                    }
                }
            } catch (e: Exception) {
                Log.e("TTS", "TTS generation failed: ${e.message}", e)
                bubbles.setGenerating(messageId, false, null)
                withContext(Dispatchers.Main) {
                    Toast.makeText(context, "TTS failed: ${e.message?.take(30)}", Toast.LENGTH_SHORT).show()
                }
            } finally {
                // D10 (Task 7.9): cancel the slow-first-byte watchdog on
                // EVERY exit path (success/failure/throw) — the job is
                // launched with stillInFlight = { true }, so cancellation
                // is the only thing that suppresses a stale "loading
                // models…" toast after a failed/thrown request.
                slowToastJob?.cancel()
                // Stop background service when TTS completes
                try {
                    val stopIntent = android.content.Intent(context, BackgroundTaskService::class.java).apply {
                        action = BackgroundTaskService.ACTION_STOP
                    }
                    context.startService(stopIntent)
                } catch (_: Exception) {}
            }
        }
    }
}

/** Play [file] once with a MediaPlayer, then release it and delete the file. */
private fun playOnceThenDelete(file: File) {
    val player = MediaPlayer()
    player.setDataSource(file.absolutePath)
    player.prepare()
    player.start()
    player.setOnCompletionListener { it.release(); file.delete() }
}
